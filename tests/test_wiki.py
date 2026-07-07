"""Unit tests for the wiki subsystem (Phase A)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.agent.wiki import (  # noqa: E402
    WikiPaths,
    WikiQuerier,
    WikiStore,
    build_wiki_tool_registry,
)
from nanobot.agent.wiki.frontmatter import (  # noqa: E402
    extract_wikilinks,
    parse_frontmatter,
    render_page,
    serialize_frontmatter,
)


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_with_yaml_block() -> None:
    text = (
        "---\n"
        'title: "Foo Bar"\n'
        "slug: foo-bar\n"
        'tags: ["alpha", "beta"]\n'
        "links: [a, b]\n"
        "created: 2026-07-07T10:00:00\n"
        "updated: 2026-07-07T10:00:00\n"
        "source: obsidian:Notes/foo.md\n"
        "---\n"
        "\n"
        "# Heading\n"
        "Body content."
    )
    fm, body = parse_frontmatter(text)
    assert fm.title == "Foo Bar"
    assert fm.slug == "foo-bar"
    assert fm.tags == ["alpha", "beta"]
    assert fm.links == ["a", "b"]
    assert fm.source == "obsidian:Notes/foo.md"
    assert "Heading" in body
    assert "Body content" in body


def test_parse_frontmatter_without_block() -> None:
    text = "# Just a heading\n\nNo frontmatter here."
    fm, body = parse_frontmatter(text)
    assert fm.title == ""
    assert fm.slug == ""
    assert body == text


def test_serialize_frontmatter_roundtrip() -> None:
    from nanobot.agent.wiki.frontmatter import WikiFrontmatter

    fm = WikiFrontmatter(title="Foo", slug="foo", tags=["a"], links=["b"])
    text = serialize_frontmatter(fm)
    parsed, _ = parse_frontmatter(text + "\nbody")
    assert parsed.title == "Foo"
    assert parsed.slug == "foo"
    assert parsed.tags == ["a"]
    assert parsed.links == ["b"]


def test_render_page_stamps_timestamps() -> None:
    from datetime import datetime, timezone
    from nanobot.agent.wiki.frontmatter import WikiFrontmatter

    fm = WikiFrontmatter(title="Foo", slug="foo")
    fixed_now = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)
    out = render_page(fm, "hello", now=fixed_now)
    assert "created: 2026-07-07T10:00:00" in out
    assert "updated: 2026-07-07T10:00:00" in out
    assert "hello" in out


def test_extract_wikilinks_basic() -> None:
    body = "See [[agent-loop]] and [[memory-subsystem|the memory layer]]. [[non-existent]]"
    links = extract_wikilinks(body)
    assert links == ["agent-loop", "memory-subsystem", "non-existent"]


def test_extract_wikilinks_dedup() -> None:
    body = "[[foo]] and [[foo]] again"
    assert extract_wikilinks(body) == ["foo"]


def test_extract_wikilinks_strips_path_prefix() -> None:
    body = "[[Notes/agent-loop]] and [[Topics/Memory/subsystem]]"
    assert extract_wikilinks(body) == ["agent-loop", "subsystem"]


# ---------------------------------------------------------------------------
# WikiPaths
# ---------------------------------------------------------------------------


def test_wiki_paths_from_workspace(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    assert paths.workspace == tmp_path.resolve()
    assert paths.pages_dir == tmp_path.resolve() / "wiki" / "pages"
    assert paths.index_file == tmp_path.resolve() / "wiki" / "index.json"
    assert paths.pages_dir.exists()
    assert paths.ima_captures_dir.exists()


def test_wiki_paths_page_path_validates_slug(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    assert paths.page_path("agent-loop") == paths.pages_dir / "agent-loop.md"
    with pytest.raises(ValueError):
        paths.page_path("Invalid Slug")
    with pytest.raises(ValueError):
        paths.page_path("123-starts-with-digit")


# ---------------------------------------------------------------------------
# WikiStore
# ---------------------------------------------------------------------------


def test_store_writes_and_reads_page(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)

    page = store.write_page(
        slug="agent-loop",
        title="Agent Loop",
        body="The agent loop is the core orchestration.",
        tags=["core", "agent"],
        source="obsidian:Notes/agent.md",
    )
    assert page.slug == "agent-loop"
    assert page.title == "Agent Loop"
    assert page.sha

    # Re-read
    fetched = store.read_page("agent-loop")
    assert fetched is not None
    assert fetched.title == "Agent Loop"
    assert "core orchestration" in fetched.body


def test_store_atomic_write_survives_corruption(tmp_path: Path) -> None:
    """A failed write (simulated by removing the tmp file) must not corrupt the page."""
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="x", title="X", body="original")
    target = paths.page_path("x")
    assert target.exists()

    # Touch a stale .tmp file — write_page should overwrite via os.replace.
    stale_tmp = target.with_suffix(".md.tmp")
    stale_tmp.write_text("STALE", encoding="utf-8")
    store.write_page(slug="x", title="X", body="updated")
    assert not stale_tmp.exists()
    page = store.read_page("x")
    assert "updated" in page.body


def test_store_persists_index_across_instances(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    WikiStore(paths).write_page(slug="a", title="A", body="alpha")
    WikiStore(paths).write_page(slug="b", title="B", body="beta")

    # New instance must rebuild from disk + index file.
    fresh = WikiStore(paths)
    pages = fresh.list_pages()
    slugs = {p["slug"] for p in pages}
    assert slugs == {"a", "b"}


def test_store_rebuild_index_when_corrupt(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="alpha", title="Alpha", body="A")

    # Corrupt the index.json
    paths.index_file.write_text("{ not valid json", encoding="utf-8")

    fresh = WikiStore(paths)
    pages = fresh.list_pages()
    assert any(p["slug"] == "alpha" for p in pages)


def test_store_extracts_wikilinks_into_links(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="a", title="A", body="see [[b]] and [[c]] for more")

    page = store.read_page("a")
    assert "b" in page.fm.links
    assert "c" in page.fm.links


def test_store_merge_existing_preserves_created(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)

    first = store.write_page(slug="x", title="X", body="body")
    second = store.write_page(slug="x", title="X renamed", body="body v2")
    assert second.fm.created == first.fm.created


def test_store_delete_is_soft(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="doomed", title="Doomed", body="bye")
    assert store.page_exists("doomed")

    assert store.delete_page("doomed") is True
    assert not store.page_exists("doomed")
    assert paths.deleted_page_path("doomed").exists()
    assert not paths.page_path("doomed").exists()


def test_store_backlinks(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="target", title="Target", body="hub")
    store.write_page(slug="a", title="A", body="links to [[target]] here")
    store.write_page(slug="b", title="B", body="and [[target]] again")

    assert set(store.backlinks("target")) == {"a", "b"}


# ---------------------------------------------------------------------------
# WikiQuerier
# ---------------------------------------------------------------------------


def test_querier_search_returns_ranked_hits(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="agent-loop", title="Agent Loop", body="The agent loop is the core orchestration engine.")
    store.write_page(slug="memory", title="Memory", body="Memory stores long-term facts in MEMORY.md.")
    store.write_page(slug="tools", title="Tools", body="Tools extend the agent's capabilities.")

    querier = WikiQuerier(store)
    hits = querier.search("agent loop", k=5)
    assert hits, "expected at least one hit"
    top = hits[0]
    assert top.slug == "agent-loop"


def test_querier_search_empty_query(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="x", title="X", body="hello")
    querier = WikiQuerier(store)
    assert querier.search("") == []
    assert querier.search("   ") == []


def test_querier_search_chinese(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="zh", title="知识库", body="知识库问答助手的核心是 wiki 和 obsidian。")
    querier = WikiQuerier(store)
    hits = querier.search("知识库")
    assert hits
    assert hits[0].slug == "zh"


def test_querier_invalidate_refreshes_index(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="a", title="A", body="alpha")
    querier = WikiQuerier(store)

    # Warm up cache.
    querier.search("alpha")
    assert querier._index is not None

    # Add a new page; cache is stale.
    store.write_page(slug="b", title="B", body="beta bravo")
    # Invalidate forces a rebuild.
    querier.invalidate()
    hits = querier.search("bravo")
    assert hits
    assert hits[0].slug == "b"


# ---------------------------------------------------------------------------
# Wiki tool registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiki_tool_registry_reader_role(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="hello", title="Hello", body="world")

    registry = build_wiki_tool_registry(store, role="reader")
    names = sorted(registry._tools.keys())
    # Reader has: list_wiki_pages, wiki_backlinks, wiki_read, wiki_search
    assert "wiki_search" in names
    assert "wiki_read" in names
    assert "list_wiki_pages" in names
    assert "wiki_backlinks" in names
    # No write tools.
    assert "write_wiki_page" not in names
    assert "update_wiki_page" not in names


@pytest.mark.asyncio
async def test_wiki_tool_registry_generator_role(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    registry = build_wiki_tool_registry(store, role="generator")
    names = sorted(registry._tools.keys())
    assert "write_wiki_page" in names
    assert "update_wiki_page" in names


@pytest.mark.asyncio
async def test_wiki_read_tool_returns_markdown(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="test", title="Test", body="hello world")
    registry = build_wiki_tool_registry(store, role="reader")

    result = await registry.execute("wiki_read", {"slug": "test"})
    assert "hello world" in result


@pytest.mark.asyncio
async def test_wiki_read_tool_missing_returns_error(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    registry = build_wiki_tool_registry(store, role="reader")

    from nanobot.agent.tools.base import ToolResult

    result = await registry.execute("wiki_read", {"slug": "nonexistent"})
    assert isinstance(result, ToolResult)
    assert result.is_error


@pytest.mark.asyncio
async def test_wiki_search_tool_returns_ranked_list(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="ai", title="AI", body="transformers, attention, agents")
    store.write_page(slug="web", title="Web", body="http servers and browsers")
    registry = build_wiki_tool_registry(store, role="reader")

    result = await registry.execute("wiki_search", {"query": "transformer", "k": "3"})
    assert "ai" in result.lower()


@pytest.mark.asyncio
async def test_wiki_write_tool_creates_page(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    registry = build_wiki_tool_registry(store, role="generator")

    result = await registry.execute(
        "write_wiki_page",
        {
            "slug": "new-page",
            "title": "New Page",
            "body": "Some body text",
            "tags": ["test", "demo"],
            "links": ["other-page"],
            "source": "test:abc",
        },
    )
    assert "Wrote wiki page" in result
    page = store.read_page("new-page")
    assert page is not None
    assert page.title == "New Page"


@pytest.mark.asyncio
async def test_wiki_update_tool_replaces_text(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="x", title="X", body="original sentence here")
    registry = build_wiki_tool_registry(store, role="generator")

    result = await registry.execute(
        "update_wiki_page",
        {
            "slug": "x",
            "old_text": "original sentence",
            "new_text": "updated sentence",
        },
    )
    assert "Updated wiki page" in result
    page = store.read_page("x")
    assert "updated sentence" in page.body


@pytest.mark.asyncio
async def test_wiki_update_tool_missing_text_returns_error(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    store.write_page(slug="x", title="X", body="original")
    registry = build_wiki_tool_registry(store, role="generator")

    from nanobot.agent.tools.base import ToolResult

    result = await registry.execute(
        "update_wiki_page",
        {"slug": "x", "old_text": "not present", "new_text": "whatever"},
    )
    assert isinstance(result, ToolResult)
    assert result.is_error