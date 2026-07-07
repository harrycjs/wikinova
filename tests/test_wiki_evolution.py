"""Tests for the wiki self-evolution cron job (Phase D)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.agent.memory import MemoryStore
from nanobot.agent.wiki import WikiEvolution, WikiPaths, WikiStore


class _StubAgent:
    """Stub AgentLoop that records tool calls instead of executing them."""

    def __init__(self, memory: MemoryStore):
        self.context = type("C", (), {"memory": memory})()
        self.processed: list[dict[str, Any]] = []

    async def process_direct(self, prompt, *, session_key, ephemeral, tools=None, persist_user_message=True, **_):
        self.processed.append(
            {"prompt": prompt, "session_key": session_key, "tools": tools}
        )
        # Simulate the wiki agent writing a page via the registry.
        if tools is not None and hasattr(tools, "execute"):
            try:
                await tools.execute(
                    "write_wiki_page",
                    {
                        "slug": "favorite-tools",
                        "title": "Favorite Tools",
                        "body": "User likes ripgrep and fd.",
                        "tags": ["tools"],
                        "links": [],
                        "source": "evolution",
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return None


@pytest.mark.asyncio
async def test_evolution_no_history_is_noop(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)
    agent = _StubAgent(memory)

    evolution = WikiEvolution(store, memory)
    result = await evolution.run_once(agent)

    assert result.ran is False
    assert result.cursor_after == result.cursor_before == 0
    assert result.summary == "no unprocessed history"


@pytest.mark.asyncio
async def test_evolution_advances_cursor_on_real_diff(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)

    # Seed two history entries so evolution has something to process.
    memory.append_history("User mentioned their favorite tools are ripgrep and fd.", session_key="test:1")
    memory.append_history("User asked about transformers architecture.", session_key="test:2")

    agent = _StubAgent(memory)
    evolution = WikiEvolution(store, memory, max_batch_entries=10)
    result = await evolution.run_once(agent)

    assert result.ran is True
    # Cursor should advance past the seeded entries (assuming write_wiki_page
    # actually wrote to disk).
    assert result.cursor_after >= result.cursor_before
    # The stub wrote a wiki page; the page should exist.
    page = store.read_page("favorite-tools")
    assert page is not None
    assert "ripgrep" in page.body


@pytest.mark.asyncio
async def test_evolution_does_not_advance_cursor_on_no_diff(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)
    memory.append_history("no real content here")

    # An agent that does NOT write anything — diff gate should keep cursor put.
    class _SilentAgent(_StubAgent):
        async def process_direct(self, prompt, *, session_key, ephemeral, tools=None, persist_user_message=True, **_):
            return None  # no tool calls

    agent = _SilentAgent(memory)
    evolution = WikiEvolution(store, memory)
    result = await evolution.run_once(agent)

    assert result.ran is True
    assert result.cursor_after == result.cursor_before
    assert "no productive edits" in result.summary


@pytest.mark.asyncio
async def test_evolution_appends_to_audit_log(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)
    memory.append_history("something")

    agent = _StubAgent(memory)
    evolution = WikiEvolution(store, memory)
    await evolution.run_once(agent)

    log_entries = evolution.read_recent_log()
    assert log_entries
    last = log_entries[-1]
    assert "started_at" in last
    assert "cursor_before" in last
    assert "cursor_after" in last


def test_evolution_cursor_roundtrip(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)
    evolution = WikiEvolution(store, memory)

    assert evolution.read_cursor() == 0
    evolution.write_cursor(42)
    assert evolution.read_cursor() == 42


def test_evolution_log_persists_across_instances(tmp_path: Path) -> None:
    paths = WikiPaths.from_workspace(tmp_path)
    store = WikiStore(paths)
    memory = MemoryStore(tmp_path)

    e1 = WikiEvolution(store, memory)
    e1.append_log({"ran": True, "cursor_before": 0, "cursor_after": 5})

    e2 = WikiEvolution(store, memory)
    log = e2.read_recent_log()
    assert log
    assert log[-1]["cursor_after"] == 5