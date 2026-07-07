"""IMA → LLM summary → Obsidian Nanobot/Inbox pipeline.

Pulls content from IMA (notes + knowledge-base items), asks an isolated LLM
turn to summarize each item into a structured note with YAML frontmatter,
and writes the result to ``<vault>/<vault_root>/<inbox_subdir>/<date>-<id>.md``.

This is the "first mile" of the data flow:

    IMA ──► LLM summary ──► Obsidian Nanobot/Inbox/*.md ──► wiki ──► Q&A

The Obsidian sync layer (Phase C) watches ``Nanobot/Inbox`` and turns new
files into wiki pages. Self-evolution (Phase D) only writes back to the wiki,
never to the inbox.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.ima._client import IMAClient, IMAError
from nanobot.security.workspace_policy import (
    WorkspaceBoundaryError,
    require_path_within,
)

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


# Prompt template lives next to this module.
_PROMPT_PATH = Path(__file__).parent / "prompts" / "ima_summarize.md"


_SUMMARY_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)",
    re.DOTALL,
)


@dataclass
class PipelineResult:
    """Outcome of one pipeline run."""

    items_processed: int = 0
    items_skipped: int = 0
    notes_written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "items_processed": self.items_processed,
            "items_skipped": self.items_skipped,
            "notes_written": list(self.notes_written),
            "errors": list(self.errors),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class IMAIngestPipeline:
    """Drive IMA → LLM summary → Obsidian Nanobot/Inbox.

    Parameters
    ----------
    client : IMAClient
        Configured IMA API client.
    provider : LLMProvider
        Provider used for the summarization LLM call.
    model : str
        Model id for the summarization calls.
    inbox_root : Path
        ``<vault>/<vault_root>/<inbox_subdir>``. Must already exist or be
        creatable; the pipeline never touches other directories under the vault.
    workspace : Path
        Nanobot workspace root, used to store the ingestion cursor.
    inbox_dir_name : str, default "Inbox"
        Sub-directory of ``vault_root`` where summaries land.
    max_concurrency : int, default 4
        Cap on concurrent LLM calls.
    """

    def __init__(
        self,
        client: IMAClient,
        provider: "LLMProvider",
        model: str,
        inbox_root: Path,
        workspace: Path,
        *,
        inbox_dir_name: str = "Inbox",
        max_concurrency: int = 4,
    ):
        self.client = client
        self.provider = provider
        self.model = model
        self.inbox_root = Path(inbox_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.inbox_dir_name = inbox_dir_name
        self.max_concurrency = max(1, max_concurrency)

        # Cursor file tracks which IMA sources we've already processed.
        self.cursor_file = self.workspace / "ima" / ".sync_cursor.json"
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)

    @property
    def inbox_dir(self) -> Path:
        """``<inbox_root>/<inbox_dir_name>`` — guaranteed inside inbox_root."""
        target = (self.inbox_root / self.inbox_dir_name).resolve()
        try:
            require_path_within(target, self.inbox_root)
        except WorkspaceBoundaryError:
            raise
        target.mkdir(parents=True, exist_ok=True)
        return target

    # -- cursor ----------------------------------------------------------

    def _read_cursor(self) -> dict[str, Any]:
        if not self.cursor_file.exists():
            return {}
        try:
            return json.loads(self.cursor_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_cursor(self, data: dict[str, Any]) -> None:
        tmp = self.cursor_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, self.cursor_file)

    # -- IMA fetch -------------------------------------------------------

    async def _fetch_notes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch the user's IMA notes (cursor-paginated, up to one full sweep)."""
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(20):  # safety bound — never loop forever
            try:
                data = await self.client.list_note(cursor=cursor, limit=limit)
            except IMAError as exc:
                logger.warning("IMA list_note failed: {}", exc)
                break
            items = data.get("note_list") or data.get("list") or []
            out.extend(items)
            if data.get("is_end") or not data.get("next_cursor") or not items:
                break
            cursor = data.get("next_cursor") or ""
        return out

    async def _fetch_kb_items(self, kb_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(20):
            try:
                data = await self.client.get_knowledge_list(kb_id=kb_id, cursor=cursor, limit=limit)
            except IMAError as exc:
                logger.warning("IMA get_knowledge_list failed: {}", exc)
                break
            items = data.get("list") or data.get("knowledge_list") or []
            out.extend(items)
            if data.get("is_end") or not data.get("next_cursor") or not items:
                break
            cursor = data.get("next_cursor") or ""
        return out

    async def _fetch_note_content(self, note_id: str) -> str:
        try:
            data = await self.client.get_doc_content(note_id=note_id, content_format=0)
        except IMAError as exc:
            logger.warning("IMA get_doc_content failed for {}: {}", note_id, exc)
            return ""
        # The response shape varies; try common keys.
        for key in ("content", "doc_content", "text"):
            if key in data and isinstance(data[key], str):
                return data[key]
        # Fallback: stringify the whole payload (truncated).
        return json.dumps(data, ensure_ascii=False)[:8000]

    # -- LLM summarization ----------------------------------------------

    async def _summarize(self, *, source_kind: str, source_id: str, source_url: str, raw_content: str) -> str | None:
        """Run the summarization LLM call; return rendered markdown or None on failure."""
        template = _PROMPT_PATH.read_text(encoding="utf-8")
        prompt = template.format(
            source_kind=source_kind,
            source_id=source_id,
            source_url=source_url or "",
            captured_at=datetime.now(timezone.utc).isoformat(),
            raw_content=(raw_content or "")[:20_000],
        )
        from nanobot.providers.base import GenerationSettings

        try:
            response = await self.provider.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                settings=GenerationSettings(max_tokens=2500, temperature=0.2),
                tools=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("IMA summarize LLM call failed: {}", exc)
            return None
        content = (response.content or "").strip()
        if not content:
            return None
        # Strip accidental code-fence wrappers.
        if content.startswith("```"):
            content = re.sub(r"\A```[a-zA-Z]*\n", "", content)
            if content.endswith("```"):
                content = content[:-3]
        return content.strip()

    # -- write to inbox -------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> None:
        import os

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        with open(target, "ab") as fh:
            fh.flush()
            os.fsync(fh.fileno())

    def _safe_slug(self, text: str, *, fallback: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
        slug = re.sub(r"-+", "-", slug)[:80].strip("-")
        return slug or fallback

    def _frontmatter_dict(self, summary: str) -> dict[str, Any] | None:
        """Parse the YAML frontmatter of a summarized note (very permissive)."""
        match = _SUMMARY_RE.match(summary)
        if not match:
            return None
        # Naive key/value extraction — frontmatter is rendered by an LLM we
        # control, so simple line-based parsing is good enough.
        fm: dict[str, Any] = {}
        for line in match.group("fm").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip().strip('"').strip("'")
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                fm[key.strip()] = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            else:
                fm[key.strip()] = value
        return fm

    # -- main entry point ------------------------------------------------

    async def run_once(self, *, max_items: int = 20) -> PipelineResult:
        """Run one ingest pass.

        Pulls the user's notes (skips already-seen ids via the cursor) and
        summarizes each one into the Obsidian inbox. Knowledge-base items can
        be added by passing ``kb_ids=[...]`` to :meth:`run_kb`.
        """
        started = datetime.now(timezone.utc).isoformat()
        result = PipelineResult(started_at=started)

        if not self.client.has_credentials():
            result.errors.append("IMA credentials missing")
            result.finished_at = datetime.now(timezone.utc).isoformat()
            return result

        cursor = self._read_cursor()
        seen_ids: set[str] = set(cursor.get("notes") or [])

        notes = await self._fetch_notes()
        new_notes = [n for n in notes if (n.get("note_id") or n.get("id")) not in seen_ids]
        new_notes = new_notes[:max_items]

        sem = asyncio.Semaphore(self.max_concurrency)

        async def process(note: dict[str, Any]) -> None:
            note_id = note.get("note_id") or note.get("id") or ""
            if not note_id:
                result.items_skipped += 1
                return
            title = note.get("title") or note.get("note_title") or ""
            async with sem:
                body = await self._fetch_note_content(note_id)
                if not body.strip():
                    result.items_skipped += 1
                    return
                summary = await self._summarize(
                    source_kind="note",
                    source_id=note_id,
                    source_url=note.get("url") or "",
                    raw_content=body,
                )
                if not summary:
                    result.items_skipped += 1
                    return
                written = self._write_summary_to_inbox(
                    summary=summary,
                    fallback_slug=self._safe_slug(title, fallback=f"ima-{note_id[:8]}"),
                )
                if written:
                    result.notes_written.append(written)
                    result.items_processed += 1
                    seen_ids.add(note_id)
                else:
                    result.items_skipped += 1

        await asyncio.gather(*(process(n) for n in new_notes), return_exceptions=True)
        cursor["notes"] = sorted(seen_ids)
        cursor["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_cursor(cursor)

        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result

    def _write_summary_to_inbox(self, *, summary: str, fallback_slug: str) -> str | None:
        """Atomically write a summarized note into the Obsidian inbox.

        Returns the on-disk relative path (POSIX, relative to inbox_root) on
        success, or None if the summary was malformed / unsafe.
        """
        fm = self._frontmatter_dict(summary)
        if not fm or not fm.get("slug"):
            slug = fallback_slug
        else:
            slug = self._safe_slug(str(fm["slug"]), fallback=fallback_slug)

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = self.inbox_dir / f"{date}-{slug}.md"
        # Ensure target is inside inbox_dir (defense in depth).
        try:
            require_path_within(target, self.inbox_root)
        except WorkspaceBoundaryError:
            logger.warning("refusing to write outside inbox: {}", target)
            return None

        try:
            self._atomic_write(target, summary)
        except OSError as exc:
            logger.warning("write to inbox failed for {}: {}", target, exc)
            return None

        rel = target.relative_to(self.inbox_root).as_posix()
        logger.info("Wrote IMA summary to {}", target)
        return rel
