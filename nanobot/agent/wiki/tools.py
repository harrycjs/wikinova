"""Wiki tools: expose wiki_search / wiki_read / wiki_write / wiki_backlinks to the agent.

Two tool roles:

- ``role="generator"`` — used by WikiGenerator and WikiEvolution. Has list,
  read, write, update, link resolution tools. Scoped to ``workspace/wiki/``.
- ``role="reader"`` — used by the main Q&A agent turn. Has only list, read,
  search, backlinks. Read-only.

The tools are registered into a fresh ``ToolRegistry`` and returned via
:func:`build_wiki_tool_registry`. The main AgentLoop merges this registry into
its own via the ``tools=`` argument on ``process_direct`` (already supported
by the runner).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import (
    StringSchema,
    tool_parameters_schema,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Search query — natural language or keywords."),
        k={"type": "string", "description": "Max hits to return.", "default": "5"},
    )
)
class WikiSearchTool(Tool):
    """Search the wiki for pages relevant to a query."""

    name = "wiki_search"
    description = (
        "Search the LLM-generated knowledge wiki for pages relevant to the "
        "given query. Returns ranked hits with slug, title, snippet, score. "
        "Use this whenever the user asks a factual question — it is your "
        "primary mechanism for grounding answers."
    )
    read_only = True
    concurrency_safe = True

    def __init__(self, store, querier):
        self._store = store
        self._querier = querier

    async def execute(self, query: str, k: str = "5", **kwargs) -> str:
        try:
            top_k = max(1, min(50, int(k)))
        except ValueError:
            top_k = 5
        try:
            hits = self._querier.search(query, k=top_k)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"wiki_search failed: {exc}")
        if not hits:
            return (
                "No wiki pages matched the query. Try a different query, "
                "or call obsidian_search to search the raw Obsidian vault."
            )
        lines = [f"Found {len(hits)} wiki pages:"]
        for i, hit in enumerate(hits, 1):
            lines.append(
                f"{i}. **{hit.title}** (slug: `{hit.slug}`, score: {hit.score:.2f})\n"
                f"   {hit.snippet}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        slug=StringSchema("Page slug (lowercase, dashes, e.g. ``agent-loop``)."),
    )
)
class WikiReadTool(Tool):
    """Read the full content of a wiki page by slug."""

    name = "wiki_read"
    description = (
        "Read the full markdown body of a wiki page by slug. Use after "
        "wiki_search to load the most relevant pages."
    )
    read_only = True
    concurrency_safe = True

    def __init__(self, store, querier):
        self._store = store
        self._querier = querier

    async def execute(self, slug: str, **kwargs) -> str:
        page = self._store.read_page(slug)
        if page is None:
            return ToolResult.error(
                f"Wiki page `{slug}` does not exist. "
                "Use wiki_search to find relevant pages, or list_wiki_pages to enumerate."
            )
        # Invalidate BM25 cache because someone may have written a new page.
        self._querier.invalidate()
        return f"# {page.title}\n\n{page.body}"


@tool_parameters(
    tool_parameters_schema()
)
class WikiListTool(Tool):
    """List all wiki pages."""

    name = "list_wiki_pages"
    description = "Enumerate all wiki pages with their slug, title, and tags."
    read_only = True
    concurrency_safe = True

    def __init__(self, store):
        self._store = store

    async def execute(self, **kwargs) -> str:
        pages = self._store.list_pages()
        if not pages:
            return "Wiki is currently empty."
        lines = [f"Wiki has {len(pages)} pages:"]
        for page in pages:
            tags = ", ".join(page.get("tags") or [])
            lines.append(f"- `{page['slug']}` — {page.get('title', page['slug'])} (tags: {tags or '—'})")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        slug=StringSchema("Page slug to find backlinks for."),
    )
)
class WikiBacklinksTool(Tool):
    """Return the slugs of pages that link to a given page."""

    name = "wiki_backlinks"
    description = "Find pages that link *to* the given slug — useful for traversal."
    read_only = True
    concurrency_safe = True

    def __init__(self, store):
        self._store = store

    async def execute(self, slug: str, **kwargs) -> str:
        links = self._store.backlinks(slug)
        if not links:
            return f"No pages link to `{slug}`."
        return f"Pages linking to `{slug}`: " + ", ".join(f"`{x}`" for x in links)


# --- Generator-only tools (write/update) -----------------------------------


@tool_parameters(
    tool_parameters_schema(
        slug=StringSchema("Page slug matching [a-z][a-z0-9-]{0,95}."),
        title=StringSchema("Human-readable page title."),
        body=StringSchema("Markdown body. Use [[other-slug]] for wikilinks."),
        tags={
            "type": "array",
            "description": "Lowercase tags.",
            "items": {"type": "string"},
            "default": [],
        },
        links={
            "type": "array",
            "description": "Slugs of related pages.",
            "items": {"type": "string"},
            "default": [],
        },
        source={"type": "string", "description": "Provenance tag.", "default": ""},
    )
)
class WikiWriteTool(Tool):
    """Create or update a wiki page (generator role only)."""

    name = "write_wiki_page"
    description = (
        "Create or overwrite a wiki page. Use only when authoring — the Q&A "
        "agent should NOT call this tool. Body uses [[wikilink]] syntax."
    )
    concurrency_safe = False

    def __init__(self, store):
        self._store = store

    async def execute(
        self,
        slug: str,
        title: str,
        body: str,
        tags: list[str] | None = None,
        links: list[str] | None = None,
        source: str = "",
        **kwargs,
    ) -> str:
        try:
            page = self._store.write_page(
                slug=slug,
                title=title,
                body=body,
                tags=tags or [],
                links=links or [],
                source=source,
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"write_wiki_page failed: {exc}")
        return f"Wrote wiki page `{slug}` (sha={page.sha[:8]}, mtime={page.mtime})."


@tool_parameters(
    tool_parameters_schema(
        slug=StringSchema("Page slug to edit."),
        old_text=StringSchema("Exact existing text to replace."),
        new_text=StringSchema("Replacement text."),
    )
)
class WikiUpdateTool(Tool):
    """Surgical edit of an existing wiki page (generator role only)."""

    name = "update_wiki_page"
    description = (
        "Replace a snippet of an existing wiki page. Use this to extend an "
        "existing page rather than rewriting it whole."
    )
    concurrency_safe = False

    def __init__(self, store):
        self._store = store

    async def execute(self, slug: str, old_text: str, new_text: str, **kwargs) -> str:
        page = self._store.read_page(slug)
        if page is None:
            return ToolResult.error(f"Wiki page `{slug}` does not exist.")
        body = page.body
        if old_text not in body:
            return ToolResult.error(
                f"old_text not found verbatim in `{slug}`. "
                "Re-read the page with wiki_read and try again."
            )
        new_body = body.replace(old_text, new_text, 1)
        try:
            updated = self._store.write_page(
                slug=slug,
                title=page.title,
                body=new_body,
                tags=list(page.fm.tags),
                links=list(page.fm.links),
                source=page.fm.source,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"update_wiki_page failed: {exc}")
        return f"Updated wiki page `{slug}` (sha={updated.sha[:8]})."


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_wiki_tool_registry(store, role: Literal["reader", "generator"] = "reader") -> ToolRegistry:
    """Build a ToolRegistry for wiki operations.

    ``role="reader"`` is for the main Q&A agent: list / search / read / backlinks.
    ``role="generator"`` adds write/update and is used by WikiGenerator and WikiEvolution.
    """
    from nanobot.agent.wiki.querier import WikiQuerier

    querier = WikiQuerier(store)

    registry = ToolRegistry()
    registry.register(WikiSearchTool(store, querier))
    registry.register(WikiReadTool(store, querier))
    registry.register(WikiListTool(store))
    registry.register(WikiBacklinksTool(store))

    if role == "generator":
        registry.register(WikiWriteTool(store))
        registry.register(WikiUpdateTool(store))

    return registry
