"""WikiEvolution: self-evolution loop that ingests conversation history into the wiki.

Mirror of the Dream pattern at ``nanobot/cli/commands.py:1405-1467``:

1. Read ``memory/history.jsonl`` entries newer than ``.evolution_cursor``.
2. Build a prompt from the wiki index + MEMORY.md + the new history batch.
3. Invoke an isolated agent turn with the restricted wiki tool registry.
4. Compute the real diff via ``WikiStore.diff()`` — gate the cursor advance
   on actual file changes, never on the LLM's self-report.

This module is intentionally small and side-effect-only: it composes a
``WikiStore``, ``WikiGenerator``, and ``MemoryStore`` that the caller wires up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Beijing timezone (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

from loguru import logger

from nanobot.agent.wiki.store import WikiStore

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.memory import MemoryStore


EVOLVE_PROMPT = """\
You are the self-evolution engine for a personal knowledge wiki. Your job is to
extract new facts, preferences, habits, named entities, and project context from
recent user/assistant conversations and crystallize them into the wiki.

The wiki lives at `workspace/wiki/pages/` (one markdown file per slug) with a
`wiki/index.json` summary index.

Current wiki pages (top 50 by title):
{wiki_index}

Long-term memory (MEMORY.md, may be empty):
{user_memory}

Recent conversation history (unprocessed since last evolution pass):
{history_batch}

Rules:
1. Run `list_wiki_pages` first to see the full picture (don't trust the truncated
   summary above).
2. For each *new* fact / preference / habit / entity / project:
   - If it maps to an existing wiki page, call `update_wiki_page` to add a
     short section at the bottom. Cite this evolution run as the source.
   - Otherwise, call `write_wiki_page` to create a fresh page. Slug must match
     `[a-z][a-z0-9-]{{0,95}}`.
3. NEVER invent facts not present in the conversation history.
4. NEVER duplicate an existing page — extend it instead.
5. ALWAYS add at least one `[[wikilink]]` to a related existing page so the
   knowledge graph keeps growing.
6. Do not write more than {max_pages} pages in this pass. If you find more
   candidates than that, pick the highest-confidence ones.
7. If the conversation history is empty or trivial, do nothing — return without
   any tool calls.

Hard limits:
- Wiki only. Do not touch `<vault>`, SOUL.md, USER.md, MEMORY.md, history.jsonl.
- No shell, exec, web, or message tools.
- Each page body must stay under 8 KB.
"""


@dataclass
class EvolutionRunResult:
    """Outcome of a single evolution pass."""

    ran: bool
    cursor_before: int
    cursor_after: int
    pages_changed: list[str]
    summary: str
    started_at: str
    finished_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "pages_changed": list(self.pages_changed),
            "summary": self.summary,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class WikiEvolution:
    """Periodic self-evolution pass over the wiki."""

    def __init__(
        self,
        store: WikiStore,
        memory: "MemoryStore",
        *,
        max_batch_entries: int = 30,
        max_pages_per_run: int = 10,
    ):
        self.store = store
        self.memory = memory
        self.max_batch_entries = max_batch_entries
        self.max_pages_per_run = max_pages_per_run

    @property
    def cursor_path(self) -> Path:
        return self.store.paths.evolution_cursor_file

    @property
    def log_path(self) -> Path:
        return self.store.paths.evolution_log_file

    def read_cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            return 0

    def write_cursor(self, value: int) -> None:
        tmp = self.cursor_path.with_suffix(self.cursor_path.suffix + ".tmp")
        tmp.write_text(str(value), encoding="utf-8")
        import os

        os.replace(tmp, self.cursor_path)

    def append_log(self, entry: dict[str, Any]) -> None:
        """Append one line to the evolution log (JSONL)."""
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()

    def read_recent_log(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    async def run_once(self, agent: "AgentLoop", *, session_key: str | None = None) -> EvolutionRunResult:
        """One evolution pass. Safe to call from a cron job."""
        from nanobot.agent.wiki.tools import build_wiki_tool_registry

        started_at = datetime.now(BEIJING_TZ).isoformat()
        cursor_before = self.read_cursor()

        # Pull unprocessed history.
        entries = self.memory.read_unprocessed_history(cursor_before)
        if not entries:
            return EvolutionRunResult(
                ran=False,
                cursor_before=cursor_before,
                cursor_after=cursor_before,
                pages_changed=[],
                summary="no unprocessed history",
                started_at=started_at,
                finished_at=datetime.now(BEIJING_TZ).isoformat(),
            )

        batch = entries[-self.max_batch_entries :]
        wiki_index = self._render_wiki_index(limit=50)
        user_memory = self.memory.read_memory() or ""
        prompt = EVOLVE_PROMPT.format(
            wiki_index=wiki_index,
            user_memory=user_memory[:4000],
            history_batch=self._format_entries(batch),
            max_pages=self.max_pages_per_run,
        )

        before_slugs = {p["slug"] for p in self.store.list_pages()}
        last_cursor = batch[-1].get("cursor", cursor_before)
        session_key = session_key or f"wiki-evolve:{started_at}"

        # Invoke the isolated turn.
        try:
            await agent.process_direct(
                prompt,
                session_key=session_key,
                ephemeral=True,
                tools=build_wiki_tool_registry(self.store, role="generator"),
                persist_user_message=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("wiki evolution turn failed; cursor NOT advanced")
            return EvolutionRunResult(
                ran=False,
                cursor_before=cursor_before,
                cursor_after=cursor_before,
                pages_changed=[],
                summary="agent turn raised",
                started_at=started_at,
                finished_at=datetime.now(BEIJING_TZ).isoformat(),
            )

        # Real diff gate.
        diff = self.store.diff()
        after_slugs = {p["slug"] for p in self.store.list_pages()}
        new_slugs = sorted(after_slugs - before_slugs)
        # ``new_slugs`` is best-effort; the gate is the diff itself.

        cursor_after = cursor_before
        summary = ""
        if diff.strip():
            cursor_after = last_cursor
            self.write_cursor(cursor_after)
            self.store.commit(f"wiki: self-evolution pass at {started_at}\n\n{diff}")
            summary = f"+{len(new_slugs)} pages, diff={len(diff)} chars"
        else:
            summary = "no productive edits; cursor unchanged"

        result = EvolutionRunResult(
            ran=True,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            pages_changed=new_slugs,
            summary=summary,
            started_at=started_at,
            finished_at=datetime.now(BEIJING_TZ).isoformat(),
        )
        self.append_log(result.to_dict())
        return result

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _format_entries(entries: list[dict[str, Any]]) -> str:
        lines = []
        for entry in entries:
            ts = entry.get("timestamp", "?")[:19]
            content = (entry.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"[{ts}] {content}")
        return "\n".join(lines)

    def _render_wiki_index(self, *, limit: int = 50) -> str:
        pages = self.store.list_pages()[:limit]
        if not pages:
            return "(wiki is empty — no existing pages)"
        lines = []
        for page in pages:
            tags = ", ".join(page.get("tags") or [])
            lines.append(f"- **{page.get('title', page['slug'])}** (`{page['slug']}`) — tags: {tags or '—'}")
        return "\n".join(lines)
