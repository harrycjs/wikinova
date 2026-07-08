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


# Load the ingest prompt template
_INGEST_PROMPT_PATH = Path(__file__).parent / "prompts" / "ingest_source.md"
INDEX_PROMPT_PATH = Path(__file__).parent / "prompts" / "generate_index.md"
LINT_PROMPT_PATH = Path(__file__).parent / "prompts" / "lint_wiki.md"


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

    async def ingest_source(
        self,
        agent: "AgentLoop",
        vault_path: Path,
        *,
        note_body: str,
        title: str | None = None,
        session_key: str | None = None,
    ) -> GenerationResult:
        """Ingest a source note and generate multiple Wiki pages.

        This is the core of the Karpathy-style wiki generation:
        one source → multiple interconnected Wiki pages.
        """
        from datetime import datetime, timezone

        title = title or vault_path.stem

        # Load the ingest prompt template
        try:
            prompt_template = _INGEST_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            # Fallback to inline prompt if file not found
            prompt_template = self._get_fallback_ingest_prompt()

        # Use str.replace() instead of .format() to avoid issues with
        # curly braces in the prompt template (e.g. {核心概念} in examples).
        prompt = (
            prompt_template
            .replace("{vault_path}", str(vault_path))
            .replace("{title}", title)
            .replace("{note_body}", note_body)
        )

        before = {p["slug"] for p in self.store.list_pages()}
        session_key = session_key or f"wiki-ingest:{datetime.now(timezone.utc).isoformat()}"

        # Run the LLM to generate multiple Wiki pages
        await agent.process_direct(
            prompt,
            session_key=session_key,
            ephemeral=True,
            tools=self._build_generator_tools(),
            persist_user_message=False,
        )

        after = {p["slug"] for p in self.store.list_pages()}
        new_pages = sorted(after - before)

        if not new_pages:
            return GenerationResult(pages_written=[], pages_updated=[], skipped_reason="no-change")

        # Update index.md after generating pages
        await self._update_index(agent, session_key=f"{session_key}:index")

        return GenerationResult(pages_written=new_pages, pages_updated=[])

    async def _update_index(self, agent: "AgentLoop", session_key: str) -> None:
        """Update the index.md file with all current pages."""
        try:
            prompt_template = INDEX_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            return

        # Get all pages for the index
        pages = self.store.list_pages()
        pages_list = "\n".join([
            f"- {p.get('title', 'Untitled')} ({p.get('slug', 'unknown')}) - {p.get('tags', [])}"
            for p in pages
        ])

        from datetime import datetime, timezone
        prompt = (
            prompt_template
            .replace("{pages_list}", pages_list)
            .replace("{timestamp}", datetime.now(timezone.utc).isoformat())
        )

        await agent.process_direct(
            prompt,
            session_key=session_key,
            ephemeral=True,
            tools=self._build_generator_tools(),
            persist_user_message=False,
        )

    async def lint_wiki(self, agent: "AgentLoop", session_key: str | None = None) -> dict[str, Any]:
        """Run a lint check on the Wiki to find and fix issues."""
        from datetime import datetime, timezone

        try:
            prompt_template = LINT_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            return {"error": "lint prompt not found"}

        # Get all pages content
        pages = self.store.list_pages()
        pages_content = []
        for p in pages[:20]:  # Limit to 20 pages for context
            slug = p.get("slug", "")
            if slug:
                page_data = self.store.read_page(slug)
                if page_data:
                    pages_content.append(f"## {page_data.title}\n{page_data.body[:2000]}")

        prompt = prompt_template.replace(
            "{pages_content}", "\n\n".join(pages_content),
        )

        session_key = session_key or f"wiki-lint:{datetime.now(timezone.utc).isoformat()}"

        await agent.process_direct(
            prompt,
            session_key=session_key,
            ephemeral=True,
            tools=self._build_generator_tools(),
            persist_user_message=False,
        )

        return {"status": "completed", "pages_checked": len(pages)}

    def _get_fallback_ingest_prompt(self) -> str:
        """Fallback prompt if the template file is not found."""
        return """\
You are the wiki generator for a personal knowledge base. The user has just
added or modified the following note in their Obsidian vault:

Path: {vault_path}
Title: {title}

--- BEGIN NOTE ---
{note_body}
--- END NOTE ---

Your job is to turn this note into multiple interconnected Wiki pages.

Generate 3-8 pages including:
1. A main topic page (required)
2. Concept pages for key terms
3. Comparison pages if applicable
4. Entity pages for people/companies

Each page must have YAML frontmatter with: title, slug, tags, type, source, created, updated, related.

Use [[slug]] syntax to link between pages. Do NOT just copy the original note - extract, reorganize, and build connections.

Available tools:
- `list_wiki_pages` — see existing pages
- `read_wiki_page(slug)` — read an existing page
- `write_wiki_page(slug, title, body, tags, links, source)` — create a page

Start generating:"""

    def _build_generator_tools(self) -> Any:
        """Build the restricted wiki-tool registry for generator turns.

        Returns a fresh ``ToolRegistry`` with list/read/write/update tools
        scoped to ``workspace/wiki/``.
        """
        # Importing here to avoid a circular import at module load.

        from nanobot.agent.wiki.tools import build_wiki_tool_registry

        return build_wiki_tool_registry(self.store, role="generator")
