"""Obsidian → wiki sync.

Walks the user's Obsidian vault (or polls it on an interval), detects files
that have changed since the last sync, and triggers :class:`WikiGenerator`
to turn them into wiki pages.

Vault is the source of truth; wiki is the cache. The sync is one-way.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from loguru import logger

from nanobot.agent.wiki.store import WikiStore
from nanobot.security.workspace_policy import WorkspaceBoundaryError, require_path_within

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


@dataclass
class SyncResult:
    scanned: int = 0
    changed: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "changed": list(self.changed),
            "generated": list(self.generated),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ObsidianWikiSync:
    """One-way vault → wiki cache.

    The ``run_once`` method scans the vault, compares each file's sha256
    against ``obsidian_state.json``, and triggers the generator for files that
    have changed or are new. Files outside the configured ``vault_root`` (the
    ``Nanobot/`` sub-tree) are passed through as well — the generator decides
    whether to write anything.
    """

    def __init__(
        self,
        store: WikiStore,
        *,
        vault_path: Path,
        vault_root: str = "Nanobot",
        on_change: Callable[[Path, str], Any] | None = None,
    ):
        self.store = store
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.vault_root = vault_root.strip("/")
        # Optional callback instead of running the generator inline. Useful for
        # tests that want to assert on what would be generated.
        self._on_change = on_change

    @property
    def state_file(self) -> Path:
        return self.store.paths.obsidian_state_file

    def _load_state(self) -> dict[str, str]:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, str]) -> None:
        import os

        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @staticmethod
    def _sha256_of(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _iter_candidate_files(self) -> Iterable[Path]:
        """Yield ``.md`` files under the vault, optionally scoped to ``vault_root``."""
        if not self.vault_path.exists():
            return
        if self.vault_root:
            scope = (self.vault_path / self.vault_root).resolve()
            try:
                require_path_within(scope, self.vault_path)
            except WorkspaceBoundaryError:
                logger.warning("obsidian sync: vault_root escapes vault, falling back to full scan")
                scope = self.vault_path
            if not scope.exists():
                return
            yield from scope.rglob("*.md")
        else:
            yield from self.vault_path.rglob("*.md")

    async def run_once(self, *, agent: "AgentLoop | None" = None, max_files: int = 25) -> SyncResult:
        started = datetime.now(timezone.utc).isoformat()
        result = SyncResult(started_at=started)

        state = self._load_state()
        candidates = list(self._iter_candidate_files())
        result.scanned = len(candidates)

        for path in candidates:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self.vault_path).as_posix()
            except ValueError:
                continue
            try:
                sha = self._sha256_of(path)
            except OSError as exc:
                result.errors.append(f"{rel}: {exc}")
                continue
            if state.get(rel) == sha:
                continue  # unchanged
            result.changed.append(rel)
            state[rel] = sha
            if len(result.changed) >= max_files:
                break

        if not result.changed:
            result.finished_at = datetime.now(timezone.utc).isoformat()
            return result

        # Trigger generation for each changed file. The generator itself decides
        # what to write into the wiki; we just feed it the file.
        from nanobot.agent.wiki.generator import WikiGenerator

        if self._on_change is not None:
            # Test path — skip the generator and call the hook instead.
            for rel in result.changed:
                try:
                    vault_file = self.vault_path / rel
                    self._on_change(vault_file, rel)
                    result.generated.append(rel)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{rel}: {exc}")
        else:
            for rel in result.changed:
                vault_file = self.vault_path / rel
                try:
                    body = vault_file.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    result.errors.append(f"{rel}: {exc}")
                    continue
                if agent is None:
                    result.skipped.append(rel)
                    continue
                gen = WikiGenerator(self.store)
                outcome = await gen.generate_from_vault_file(
                    agent,
                    vault_path=vault_file,
                    note_body=body,
                    title=vault_file.stem,
                )
                result.generated.extend(outcome.pages_written)
                if outcome.skipped_reason:
                    result.skipped.append(rel)

        self._save_state(state)
        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result
