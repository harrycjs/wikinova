"""Tests for the Obsidian read-only tools and the wiki sync module."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.agent.wiki import WikiPaths, WikiStore
from nanobot.agent.wiki.sync import ObsidianWikiSync


# ---------------------------------------------------------------------------
# ObsidianWikiSync
# ---------------------------------------------------------------------------


def _seed_vault(vault: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def test_sync_detects_changed_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    _seed_vault(
        vault,
        {
            "Nanobot/Inbox/a.md": "alpha",
            "Nanobot/Inbox/b.md": "bravo",
        },
    )
    paths = WikiPaths.from_workspace(workspace)
    store = WikiStore(paths)
    sync = ObsidianWikiSync(store, vault_path=vault, vault_root="Nanobot")

    result = asyncio.run(sync.run_once(agent=None))
    assert result.scanned == 2
    assert sorted(result.changed) == ["Nanobot/Inbox/a.md", "Nanobot/Inbox/b.md"]
    # No agent → everything skipped.
    assert result.skipped == result.changed
    assert result.generated == []


def test_sync_skips_unchanged_files_on_second_run(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    _seed_vault(vault, {"Nanobot/Inbox/a.md": "alpha"})
    paths = WikiPaths.from_workspace(workspace)
    store = WikiStore(paths)
    sync = ObsidianWikiSync(store, vault_path=vault, vault_root="Nanobot")

    first = asyncio.run(sync.run_once(agent=None))
    assert len(first.changed) == 1

    second = asyncio.run(sync.run_once(agent=None))
    assert second.changed == []


def test_sync_detects_content_modification(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    a = vault / "Nanobot" / "Inbox" / "a.md"
    a.parent.mkdir(parents=True)
    a.write_text("v1", encoding="utf-8")
    paths = WikiPaths.from_workspace(workspace)
    store = WikiStore(paths)
    sync = ObsidianWikiSync(store, vault_path=vault, vault_root="Nanobot")

    first = asyncio.run(sync.run_once(agent=None))
    assert len(first.changed) == 1

    a.write_text("v2", encoding="utf-8")
    second = asyncio.run(sync.run_once(agent=None))
    assert second.changed == ["Nanobot/Inbox/a.md"]


def test_sync_vault_root_escapes_are_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    # vault_root resolves to outside vault (via "..") — should fall back.
    paths = WikiPaths.from_workspace(workspace)
    store = WikiStore(paths)
    sync = ObsidianWikiSync(store, vault_path=vault, vault_root="../../etc")
    # No files should be enumerated; the sync stays safe.
    result = asyncio.run(sync.run_once(agent=None))
    assert result.scanned == 0
    assert result.changed == []


def test_sync_uses_callback_in_test_mode(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    _seed_vault(vault, {"Nanobot/Inbox/a.md": "alpha"})
    paths = WikiPaths.from_workspace(workspace)
    store = WikiStore(paths)
    received: list[Path] = []
    sync = ObsidianWikiSync(
        store,
        vault_path=vault,
        vault_root="Nanobot",
        on_change=lambda path, rel: received.append(path),
    )

    result = asyncio.run(sync.run_once(agent=None))
    assert len(received) == 1
    assert received[0].name == "a.md"
    assert "Nanobot/Inbox/a.md" in result.generated


def test_sync_persists_state_across_instances(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "ws"
    _seed_vault(vault, {"Nanobot/Inbox/a.md": "alpha"})
    paths = WikiPaths.from_workspace(workspace)
    WikiStore(paths)  # creates dirs
    sync1 = ObsidianWikiSync(WikiStore(paths), vault_path=vault, vault_root="Nanobot")
    asyncio.run(sync1.run_once(agent=None))

    # New instance reads the persisted state.
    sync2 = ObsidianWikiSync(WikiStore(paths), vault_path=vault, vault_root="Nanobot")
    result = asyncio.run(sync2.run_once(agent=None))
    assert result.changed == []


# ---------------------------------------------------------------------------
# Obsidian tools (smoke tests, executed through the registry)
# ---------------------------------------------------------------------------


def _make_config(vault_path: str | None):
    """Build a minimal tool-config dict matching the Obsidian tools' ctx.config shape."""
    from nanobot.config.wiki_schema import ObsidianToolsConfig

    return ObsidianToolsConfig(enabled=True, vault_path=vault_path, mode="filesystem")


class _StubContext:
    def __init__(self, cfg):
        self.config = cfg


async def _register_and_execute(tool_cls, tool_name: str, params: dict, ctx):
    """Build the tool via ``create(ctx)`` (so vault path is set) and execute."""
    from nanobot.agent.tools.registry import ToolRegistry

    instance = tool_cls.create(ctx)  # type: ignore[arg-type]
    registry = ToolRegistry()
    registry.register(instance)
    return await registry.execute(tool_name, params)


@pytest.mark.asyncio
async def test_obsidian_read_returns_file_body(tmp_path: Path) -> None:
    from nanobot.agent.tools.obsidian import ObsidianReadTool

    vault = tmp_path / "vault"
    (vault / "Nanobot").mkdir(parents=True)
    target = vault / "Nanobot" / "foo.md"
    target.write_text("hello world", encoding="utf-8")

    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianReadTool, "obsidian_read", {"path": "Nanobot/foo.md"}, ctx)
    assert "hello world" in result


@pytest.mark.asyncio
async def test_obsidian_read_blocks_path_traversal(tmp_path: Path) -> None:
    from nanobot.agent.tools.base import ToolResult
    from nanobot.agent.tools.obsidian import ObsidianReadTool

    vault = tmp_path / "vault"
    vault.mkdir()
    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianReadTool, "obsidian_read", {"path": "../escape.md"}, ctx)
    assert isinstance(result, ToolResult)
    assert result.is_error


@pytest.mark.asyncio
async def test_obsidian_list_returns_files(tmp_path: Path) -> None:
    from nanobot.agent.tools.obsidian import ObsidianListTool

    vault = tmp_path / "vault"
    (vault / "Nanobot").mkdir(parents=True)
    (vault / "Nanobot" / "a.md").write_text("a", encoding="utf-8")
    (vault / "Nanobot" / "b.md").write_text("b", encoding="utf-8")

    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianListTool, "obsidian_list", {"glob": "**/*.md"}, ctx)
    import json

    payload = json.loads(result)
    paths = {f["path"] for f in payload["files"]}
    assert paths == {"Nanobot/a.md", "Nanobot/b.md"}


@pytest.mark.asyncio
async def test_obsidian_search_finds_matching_file(tmp_path: Path) -> None:
    from nanobot.agent.tools.obsidian import ObsidianSearchTool

    vault = tmp_path / "vault"
    (vault / "Nanobot").mkdir(parents=True)
    (vault / "Nanobot" / "deep-learning.md").write_text(
        "Deep learning uses neural networks with many layers.",
        encoding="utf-8",
    )
    (vault / "Nanobot" / "cooking.md").write_text("Pasta recipes", encoding="utf-8")

    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianSearchTool, "obsidian_search", {"query": "neural"}, ctx)
    import json

    payload = json.loads(result)
    paths = [h["path"] for h in payload["hits"]]
    assert "Nanobot/deep-learning.md" in paths


@pytest.mark.asyncio
async def test_obsidian_search_handles_chinese(tmp_path: Path) -> None:
    from nanobot.agent.tools.obsidian import ObsidianSearchTool

    vault = tmp_path / "vault"
    (vault / "Nanobot").mkdir(parents=True)
    (vault / "Nanobot" / "深度学习.md").write_text(
        "深度学习是机器学习的一个分支，使用神经网络。",
        encoding="utf-8",
    )

    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianSearchTool, "obsidian_search", {"query": "深度学习"}, ctx)
    import json

    payload = json.loads(result)
    assert any("深度学习" in h["path"] for h in payload["hits"])


@pytest.mark.asyncio
async def test_obsidian_backlinks_finds_references(tmp_path: Path) -> None:
    from nanobot.agent.tools.obsidian import ObsidianBacklinksTool

    vault = tmp_path / "vault"
    (vault / "Nanobot").mkdir(parents=True)
    (vault / "Nanobot" / "agent-loop.md").write_text("# Agent Loop", encoding="utf-8")
    (vault / "Nanobot" / "memory.md").write_text(
        "See [[agent-loop]] for context.",
        encoding="utf-8",
    )

    ctx = _StubContext(_make_config(str(vault)))
    result = await _register_and_execute(ObsidianBacklinksTool, "obsidian_get_backlinks", {"slug": "agent-loop"}, ctx)
    import json

    payload = json.loads(result)
    paths = [b["path"] for b in payload["backlinks"]]
    assert "Nanobot/memory.md" in paths


@pytest.mark.asyncio
async def test_obsidian_tools_error_when_vault_not_configured(tmp_path: Path) -> None:
    from nanobot.agent.tools.base import ToolResult
    from nanobot.agent.tools.obsidian import ObsidianReadTool

    ctx = _StubContext(_make_config(vault_path=None))
    result = await _register_and_execute(ObsidianReadTool, "obsidian_read", {"path": "Nanobot/foo.md"}, ctx)
    assert isinstance(result, ToolResult)
    assert result.is_error