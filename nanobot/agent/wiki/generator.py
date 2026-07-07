"""WikiGenerator: produce wiki pages from source material using an isolated agent turn.

Mirrors the structure of ``MemoryStore.build_dream_tools``: a narrow tool
registry that only allows reading the wiki and writing new pages. The agent
turn is invoked via ``AgentLoop.process_direct`` with ``ephemeral=True`` so it
doesn't pollute the user's session history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.wiki.store import WikiStore

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


GENERATE_FROM_VAULT_PROMPT = """\
You are the wiki generator for a personal knowledge base. The user has just
added or modified the following note in their Obsidian vault:

Path: {vault_path}
Title: {title}

--- BEGIN NOTE ---
{note_body}
--- END NOTE ---

Your job is to turn this note into one or more interconnected wiki pages under
`workspace/wiki/pages/`. Each page must:

1. Have a slug matching `[a-z][a-z0-9-]{{0,95}}` derived from the note's main
   concept (lowercase, dashes for spaces).
2. Start with a YAML frontmatter block (use the `write_file` tool with the
   frontmatter rendered as the first lines of the file):
   - `title`: human-readable title
   - `slug`: the page slug
   - `tags`: 2–6 lowercase tags
   - `links`: slugs of related wiki pages (use `[[wikilink]]` syntax in the body)
   - `created` / `updated`: ISO 8601 timestamps
   - `source`: `obsidian:{vault_path}`
3. Body in markdown, with `[[other-slug]]` wikilinks to other wiki pages
   whenever a related concept is mentioned. New pages referenced in wikilinks
   do not need to be created now — they will be generated in future runs.
4. Cross-link to at least 2 existing wiki pages if any exist. Run
   `list_wiki_pages` first to see what's there.

Hard rules:
- Do NOT modify any file under `<vault>` — that is the user's primary notes.
- Do NOT modify any wiki page other than the ones you are creating.
- Do NOT use shell, exec, web_fetch, or any non-wiki tool.
- If the note is empty or trivial, do nothing — return without writing.
- Keep each page under 8 KB of body content.

Available tools (in addition to the wiki tools above):
- `list_wiki_pages` — see existing pages
- `read_wiki_page(slug)` — read an existing page for context
- `write_wiki_page(slug, title, body, tags, links)` — create a page
- `update_wiki_page(slug, old_text, new_text)` — surgical edit
"""


@dataclass
class GenerationResult:
    """Outcome of one generation run."""

    pages_written: list[str]
    pages_updated: list[str]
    skipped_reason: str | None = None


class WikiGenerator:
    """Drive wiki generation from a source note via an isolated agent turn."""

    def __init__(self, store: WikiStore):
        self.store = store

    async def generate_from_vault_file(
        self,
        agent: "AgentLoop",
        vault_path: Path,
        *,
        note_body: str,
        title: str | None = None,
        session_key: str | None = None,
    ) -> GenerationResult:
        """One-shot generation pass for a single vault file."""
        from datetime import datetime, timezone

        title = title or vault_path.stem
        prompt = GENERATE_FROM_VAULT_PROMPT.format(
            vault_path=str(vault_path),
            title=title,
            note_body=note_body,
        )
        before = {p["slug"] for p in self.store.list_pages()}
        session_key = session_key or f"wiki-gen:{datetime.now(timezone.utc).isoformat()}"

        # Snapshot pages before so we can diff after.
        await agent.process_direct(
            prompt,
            session_key=session_key,
            ephemeral=True,
            tools=self._build_generator_tools(),
            persist_user_message=False,
        )
        after = {p["slug"] for p in self.store.list_pages()}
        new_pages = sorted(after - before)
        # "updated" detection would require per-page mtime diff — for v1 we
        # only report new pages; updates show up as new pages with the same slug.
        if not new_pages:
            return GenerationResult(pages_written=[], pages_updated=[], skipped_reason="no-change")
        return GenerationResult(pages_written=new_pages, pages_updated=[])

    def _build_generator_tools(self) -> Any:
        """Build the restricted wiki-tool registry for generator turns.

        Returns a fresh ``ToolRegistry`` with list/read/write/update tools
        scoped to ``workspace/wiki/``.
        """
        # Importing here to avoid a circular import at module load.

        from nanobot.agent.wiki.tools import build_wiki_tool_registry

        return build_wiki_tool_registry(self.store, role="generator")
