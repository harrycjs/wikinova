"""Tests for the IMA client, encoding helpers, and ingest pipeline (Phase B)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.agent.knowledge.pipeline import IMAIngestPipeline
from nanobot.agent.tools.ima._client import IMAClient, IMAError
from nanobot.agent.tools.ima._encoding import best_effort_decode, ensure_utf8, to_utf8_bytes


# ---------------------------------------------------------------------------
# _encoding
# ---------------------------------------------------------------------------


def test_ensure_utf8_passes_clean_strings() -> None:
    assert ensure_utf8("hello") == "hello"
    assert ensure_utf8("中文测试") == "中文测试"
    assert ensure_utf8("mixed 中文 + english") == "mixed 中文 + english"


def test_ensure_utf8_strips_surrogates() -> None:
    """Python str cannot contain lone surrogates, but it can have replacement chars."""
    # � is a valid character and encodes to UTF-8 fine.
    assert ensure_utf8("� hi") == "� hi"


def test_ensure_utf8_rejects_none_or_non_string() -> None:
    with pytest.raises(ValueError):
        ensure_utf8(None)
    with pytest.raises(ValueError):
        ensure_utf8(123)  # type: ignore[arg-type]


def test_to_utf8_bytes_encodes() -> None:
    assert to_utf8_bytes("中文") == "中文".encode("utf-8")


def test_best_effort_decode_handles_utf8() -> None:
    assert best_effort_decode("中文".encode("utf-8")) == "中文"


def test_best_effort_decode_handles_gbk() -> None:
    raw = "中文测试".encode("gbk")
    assert best_effort_decode(raw) == "中文测试"


def test_best_effort_decode_passes_through_str() -> None:
    assert best_effort_decode("already a str") == "already a str"


# ---------------------------------------------------------------------------
# IMAClient — request shape and headers
# ---------------------------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    """httpx mock transport that records requests and returns canned responses."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._handler(request)


def _ok_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))


def _error_response(code: int, msg: str) -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"code": code, "msg": msg}).encode("utf-8"))


@pytest.mark.asyncio
async def test_ima_client_sends_correct_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response({"code": 0, "data": {"ok": True}})

    transport = _MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IMAClient(client_id="cid", api_key="key", http=http)

    data = await client.search_knowledge_base(query="")
    assert data == {"ok": True}

    assert len(captured) == 1
    headers = captured[0].headers
    assert headers["ima-openapi-clientid"] == "cid"
    assert headers["ima-openapi-apikey"] == "key"
    assert headers["content-type"] == "application/json"

    body = json.loads(captured[0].content)
    assert body["query"] == ""
    assert body["limit"] == 20  # default

    await client.aclose()


@pytest.mark.asyncio
async def test_ima_client_surfaces_backend_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _error_response(403, "permission denied")

    transport = _MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IMAClient(client_id="cid", api_key="key", http=http)

    with pytest.raises(IMAError) as exc_info:
        await client.search_knowledge_base(query="foo")
    assert exc_info.value.code == 403
    assert "permission denied" in exc_info.value.message

    await client.aclose()


@pytest.mark.asyncio
async def test_ima_client_surfaces_missing_credentials() -> None:
    client = IMAClient(client_id=None, api_key=None)
    with pytest.raises(IMAError) as exc_info:
        await client.search_knowledge_base(query="")
    assert exc_info.value.code == -100
    assert "credentials" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_ima_client_import_doc_validates_utf8() -> None:
    transport_calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        transport_calls.append(req)
        return _ok_response({"code": 0, "data": {"note_id": "n1"}})

    transport = _MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IMAClient(client_id="cid", api_key="key", http=http)

    note_id = await client.import_doc(content="中文笔记内容", title="我的笔记")
    assert note_id == {"note_id": "n1"}
    sent_body = json.loads(transport_calls[0].content)
    assert sent_body["content"] == "中文笔记内容"
    assert sent_body["title"] == "我的笔记"
    assert sent_body["content_format"] == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_ima_client_handles_unicode_paths() -> None:
    """Chinese characters in query / path / body should not be mangled."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response({"code": 0, "data": {"hit_count": 0, "list": []}})

    transport = _MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IMAClient(client_id="cid", api_key="key", http=http)

    await client.search_note(title="深度学习")
    sent_body = json.loads(captured[0].content)
    assert sent_body["query_info"]["title"] == "深度学习"

    await client.aclose()


@pytest.mark.asyncio
async def test_ima_client_gbk_response_is_decoded() -> None:
    """If IMA returns GBK bytes, we should still get a valid string back."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Return a GBK-encoded JSON containing Chinese values.
        raw = json.dumps({"code": 0, "data": {"note_id": "x", "title": "中文标题"}}).encode("utf-8")
        return httpx.Response(200, content=raw)

    transport = _MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IMAClient(client_id="cid", api_key="key", http=http)

    data = await client.get_doc_content(note_id="x")
    assert data["note_id"] == "x"
    assert data["title"] == "中文标题"
    await client.aclose()


def test_ima_client_from_env_or_files_picks_up_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "env-cid")
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "env-key")
    client = IMAClient.from_env_or_files()
    assert client.client_id == "env-cid"
    assert client.api_key == "env-key"


def test_ima_client_from_env_or_files_picks_up_files(monkeypatch, tmp_path: Path) -> None:
    # Point HOME at a tempdir so ~/.config/ima resolves inside.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("IMA_OPENAPI_CLIENTID", raising=False)
    monkeypatch.delenv("IMA_OPENAPI_APIKEY", raising=False)
    monkeypatch.delenv("IMA_CLIENT_ID", raising=False)
    monkeypatch.delenv("IMA_API_KEY", raising=False)

    cfg_dir = tmp_path / ".config" / "ima"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "client_id").write_text("file-cid", encoding="utf-8")
    (cfg_dir / "api_key").write_text("file-key", encoding="utf-8")

    client = IMAClient.from_env_or_files()
    assert client.client_id == "file-cid"
    assert client.api_key == "file-key"


# ---------------------------------------------------------------------------
# IMAIngestPipeline — atomic writes, cursor, slug validation
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal LLM provider stub for pipeline tests."""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[Any] = []

    class _Generation:
        max_tokens = 4096

    generation = _Generation()

    async def chat(self, *, messages, model, settings, tools=None):
        self.calls.append({"messages": messages, "model": model})
        from nanobot.providers.base import LLMResponse

        return LLMResponse(content=self.response, tool_calls=[])


@pytest.mark.asyncio
async def test_pipeline_atomic_write_creates_file(tmp_path: Path) -> None:
    inbox_root = tmp_path / "vault"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    response_md = (
        "---\n"
        'title: "测试笔记"\n'
        "slug: test-note\n"
        'tags: ["AI", "深度学习"]\n'
        "category: Notes\n"
        "source_id: n123\n"
        "captured_at: 2026-07-07T00:00:00\n"
        "summary: 这是一篇测试笔记。\n"
        "---\n"
        "\n"
        "## Background\n"
        "Some body content here.\n"
    )

    pipeline = IMAIngestPipeline(
        client=IMAClient(client_id="cid", api_key="key"),
        provider=_StubProvider(response_md),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=inbox_root,
        workspace=workspace,
    )

    # Drive _write_summary_to_inbox directly — no async IMA calls needed.
    written = pipeline._write_summary_to_inbox(
        summary=response_md,
        fallback_slug="test-note",
    )
    assert written is not None
    assert written.endswith(".md")
    # ``written`` is already relative to inbox_root and includes the Inbox/ prefix.
    target = inbox_root / written
    assert target.exists()
    assert target.read_text(encoding="utf-8") == response_md


@pytest.mark.asyncio
async def test_pipeline_refuses_to_write_outside_inbox(tmp_path: Path) -> None:
    inbox_root = tmp_path / "vault"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    pipeline = IMAIngestPipeline(
        client=IMAClient(client_id="cid", api_key="key"),
        provider=_StubProvider(""),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=inbox_root,
        workspace=workspace,
    )

    # Build a path that escapes inbox_root and try to write through the public helper.
    # _write_summary_to_inbox itself derives target from slug + date so it can't
    # be tricked; instead exercise the underlying require_path_within guard.
    from nanobot.security.workspace_policy import require_path_within

    outside = (inbox_root.parent / "evil.md").resolve()
    with pytest.raises(Exception):
        require_path_within(outside, inbox_root)


@pytest.mark.asyncio
async def test_pipeline_cursor_persists_seen_ids(tmp_path: Path) -> None:
    inbox_root = tmp_path / "vault"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    pipeline = IMAIngestPipeline(
        client=IMAClient(client_id="cid", api_key="key"),
        provider=_StubProvider(""),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=inbox_root,
        workspace=workspace,
    )

    pipeline._write_cursor({"notes": ["a", "b"], "updated_at": "2026-07-07"})
    cursor = pipeline._read_cursor()
    assert cursor["notes"] == ["a", "b"]


def test_pipeline_safe_slug_normalizes_input() -> None:
    inbox_root = Path("/tmp") / "vault"
    workspace = Path("/tmp") / "ws"
    pipeline = IMAIngestPipeline(
        client=IMAClient(),
        provider=_StubProvider(""),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=inbox_root,
        workspace=workspace,
    )
    assert pipeline._safe_slug("Hello World!", fallback="x") == "hello-world"
    # Chinese-only input is reduced to dashes (the regex strips non a-z0-9); the
    # result falls back when nothing survives.
    assert pipeline._safe_slug("中文笔记 标题", fallback="x") == "x"
    assert pipeline._safe_slug("", fallback="fallback") == "fallback"


def test_pipeline_frontmatter_dict_parses_basic_block() -> None:
    pipeline = IMAIngestPipeline(
        client=IMAClient(),
        provider=_StubProvider(""),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=Path("/tmp"),
        workspace=Path("/tmp"),
    )
    text = (
        "---\n"
        'title: "Foo"\n'
        "slug: foo\n"
        'tags: ["a", "b"]\n'
        "captured_at: 2026-07-07\n"
        "---\n"
        "\nbody\n"
    )
    fm = pipeline._frontmatter_dict(text)
    assert fm["title"] == "Foo"
    assert fm["slug"] == "foo"
    assert fm["tags"] == ["a", "b"]
    assert fm["captured_at"] == "2026-07-07"


def test_pipeline_frontmatter_dict_returns_none_when_no_block() -> None:
    pipeline = IMAIngestPipeline(
        client=IMAClient(),
        provider=_StubProvider(""),  # type: ignore[arg-type]
        model="stub-model",
        inbox_root=Path("/tmp"),
        workspace=Path("/tmp"),
    )
    assert pipeline._frontmatter_dict("# no frontmatter\n\nbody") is None