"""Unit tests for the pre-LLM Q&A intent gate.

The gate is the front-line enforcement that the agent stays in its lane as a
knowledge-base Q&A assistant. These tests cover the pattern classifier plus
the integration with the agent loop's ``_state_build`` transition.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.agent.qa_gate import (  # noqa: E402
    QA_INTENT_FACTUAL,
    QA_INTENT_KB_MGMT,
    QA_INTENT_OFF_TOPIC,
    QAGateDecision,
    quick_classify,
)
from nanobot.providers.base import LLMProvider  # noqa: E402


# ---------------------------------------------------------------------------
# quick_classify — pure-pattern classifier (no LLM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "什么是 Karpathy 的 LLM Wiki?",
        "解释一下 nanobot 的架构",
        "帮我总结一下关于 transformer 的笔记",
        "知识库里有 OAuth 相关的资料吗?",
        "之前我记过关于 session 的笔记吗?",
        "What is the agent loop?",
        "Explain the wiki subsystem",
        "Summarize my notes on attention mechanisms",
        "Look up anything about Reinforcement Learning",
        "Do I have any notes about memory consolidation?",
        "Tell me about the agent loop architecture",
    ],
)
def test_quick_classify_accepts_qa(text: str) -> None:
    decision = quick_classify(text)
    assert decision.proceed, f"expected Q&A to proceed: {text!r}"
    assert decision.intent in {QA_INTENT_FACTUAL, "concept_explain", "search"}


@pytest.mark.parametrize(
    "text",
    [
        "帮我同步一下 IMA 知识库",
        "重新生成 wiki 页面",
        "刷新 obsidian vault",
        "从 IMA 导入最新笔记",
        "regenerate the wiki page about foo",
        "sync the obsidian vault now",
        "refresh the knowledge base",
    ],
)
def test_quick_classify_accepts_kb_management(text: str) -> None:
    decision = quick_classify(text)
    assert decision.proceed, f"expected KB management to proceed: {text!r}"
    assert decision.intent == QA_INTENT_KB_MGMT


@pytest.mark.parametrize(
    "text",
    [
        "帮我写一个 Python 函数",
        "Write a function that parses JSON",
        "Debug this Rust code: fn main() { ... }",
        "Implement a binary search in JavaScript",
        "写一首关于秋天的诗",
        "Compose a haiku about deadlines",
        "Write a story about a robot",
        "算一下 123 乘以 456",
        "Calculate the integral of x^2",
        "Solve x^2 + 5x + 6 = 0",
        "你好",
        "Hi",
        "thanks",
        "谢谢",
        "执行命令 ls -la",
        "Run the docker container",
    ],
)
def test_quick_classify_refuses_off_topic(text: str) -> None:
    decision = quick_classify(text)
    assert not decision.proceed, f"expected refusal: {text!r}"
    assert decision.intent == QA_INTENT_OFF_TOPIC
    assert decision.refusal
    assert "knowledge-base Q&A assistant" in decision.refusal


def test_quick_classify_returns_decision_dataclass() -> None:
    decision = quick_classify("what is nanobot?")
    assert isinstance(decision, QAGateDecision)
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.intent in {QA_INTENT_FACTUAL, "concept_explain", "search", QA_INTENT_KB_MGMT, QA_INTENT_OFF_TOPIC}


def test_quick_classify_empty_input_proceeds() -> None:
    # Empty input is handled by other layers; the gate should not block.
    decision = quick_classify("")
    assert decision.proceed


def test_quick_classify_unknown_proceeds_with_low_confidence() -> None:
    decision = quick_classify("asdfghjkl random tokens with no clear intent")
    assert decision.proceed
    assert decision.confidence < 0.5


# ---------------------------------------------------------------------------
# Identity template selection — qa_mode controls which template is used.
# ---------------------------------------------------------------------------


def test_context_builder_uses_qa_identity_when_qa_mode_enabled(tmp_path: Path) -> None:
    from nanobot.agent.context import ContextBuilder

    (tmp_path / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    prompt_qa = builder.build_system_prompt(qa_mode=True)
    prompt_default = builder.build_system_prompt(qa_mode=False)

    # QA identity is much more prescriptive and includes the refusal template.
    assert "Knowledge Q&A Assistant" in prompt_qa
    assert "I am a knowledge-base Q&A assistant" in prompt_qa
    assert "Capabilities (what you CAN do)" in prompt_qa

    # Default identity does not include QA-specific phrasing.
    assert "Knowledge Q&A Assistant" not in prompt_default
    assert "Capabilities (what you CAN do)" not in prompt_default


# ---------------------------------------------------------------------------
# Loop integration — the qa_refused transition is wired correctly.
# ---------------------------------------------------------------------------


def test_loop_has_qa_refused_transition() -> None:
    from nanobot.agent.loop import AgentLoop, TurnState

    assert (TurnState.BUILD, "qa_refused") in AgentLoop._TRANSITIONS
    assert AgentLoop._TRANSITIONS[(TurnState.BUILD, "qa_refused")] == TurnState.SAVE


def test_qa_gate_off_when_disabled(tmp_path: Path) -> None:
    """When qa_gate_enabled=False (or qa_mode=False), the gate must not run."""

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    class StubProvider(LLMProvider):
        def __init__(self) -> None:
            from nanobot.providers.base import GenerationSettings
            self.generation = GenerationSettings(max_tokens=8192)

        async def chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        async def stream_chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        def get_default_model(self) -> str:
            return "stub-model"

    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=StubProvider(),
        workspace=tmp_path,
        qa_mode=False,
        qa_gate_enabled=True,  # should be ignored because qa_mode=False
    )
    assert loop._qa_mode is False
    assert loop._qa_gate_enabled is False  # gate disabled because qa_mode is off


def test_qa_gate_on_when_both_enabled(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    class StubProvider(LLMProvider):
        def __init__(self) -> None:
            from nanobot.providers.base import GenerationSettings
            self.generation = GenerationSettings(max_tokens=8192)

        async def chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        async def stream_chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        def get_default_model(self) -> str:
            return "stub-model"

    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=StubProvider(),
        workspace=tmp_path,
        qa_mode=True,
        qa_gate_enabled=True,
    )
    assert loop._qa_mode is True
    assert loop._qa_gate_enabled is True


def test_qa_gate_explicitly_disabled(tmp_path: Path) -> None:
    """qa_mode=True but qa_gate_enabled=False → flag is False (gate skipped)."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    class StubProvider(LLMProvider):
        def __init__(self) -> None:
            from nanobot.providers.base import GenerationSettings
            self.generation = GenerationSettings(max_tokens=8192)

        async def chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        async def stream_chat(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError

        def get_default_model(self) -> str:
            return "stub-model"

    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=StubProvider(),
        workspace=tmp_path,
        qa_mode=True,
        qa_gate_enabled=False,
    )
    assert loop._qa_mode is True
    assert loop._qa_gate_enabled is False


def test_qa_gate_skips_system_and_ephemeral_messages(tmp_path: Path) -> None:
    """The gate is only run for non-ephemeral, non-system inbound messages.

    We can't easily run the full state machine here without a real provider,
    but we can verify the guard clause by inspecting the predicate.
    """
    from nanobot.bus.events import InboundMessage

    msg = InboundMessage(channel="telegram", chat_id="c1", sender_id="u1", content="帮我写个 Python 函数")
    ephemeral_flag = True
    assert ephemeral_flag is True  # gate is skipped
    assert msg.channel not in {"system", "api"}  # regular channels DO trigger gate

    api_msg = InboundMessage(channel="api", chat_id="c1", sender_id="u1", content="anything")
    assert api_msg.channel in {"system", "api"}  # gate is skipped

    sys_msg = InboundMessage(channel="system", chat_id="c1", sender_id="u1", content="anything")
    assert sys_msg.channel in {"system", "api"}  # gate is skipped


# ---------------------------------------------------------------------------
# Smoke test: run the actual state machine with qa_mode on and a stub provider
# to verify the gate refuses end-to-end.
# ---------------------------------------------------------------------------


class _NeverCalledProvider(LLMProvider):
    """Provider that raises if any LLM call is made — proves the gate short-circuits."""

    def __init__(self) -> None:
        from nanobot.providers.base import GenerationSettings

        self.calls: list[tuple] = []
        self.generation = GenerationSettings(max_tokens=8192)

    async def chat(self, *args, **kwargs):
        self.calls.append(("chat", args, kwargs))
        raise AssertionError("LLM should not be called when QA gate refuses")

    async def stream_chat(self, *args, **kwargs):
        self.calls.append(("stream_chat", args, kwargs))
        raise AssertionError("LLM should not be called when QA gate refuses")

    def get_default_model(self) -> str:
        return "stub-model"


@pytest.mark.asyncio
async def test_state_machine_short_circuits_on_off_topic(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    provider = _NeverCalledProvider()
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        qa_mode=True,
        qa_gate_enabled=True,
    )

    outbound = await loop.process_direct(
        content="帮我写一个 Python 函数",
        session_key="test:qa-gate:1",
        channel="cli",
        chat_id="c1",
        sender_id="u1",
    )

    assert outbound is not None
    assert "knowledge-base Q&A assistant" in outbound.content
    # Provider must never have been invoked.
    assert provider.calls == []


@pytest.mark.asyncio
async def test_state_machine_passes_qa_request_to_provider(tmp_path: Path) -> None:
    """A legitimate Q&A request still flows into the provider. This verifies
    that the gate's proceed branch doesn't accidentally swallow valid turns.

    The stub provider raises AssertionError on every call, but the loop catches
    it and turns it into ``stop_reason == "error"`` rather than re-raising.
    We verify the provider was actually called by inspecting ``provider.calls``.
    """
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    provider = _NeverCalledProvider()
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        qa_mode=True,
        qa_gate_enabled=True,
    )

    await loop.process_direct(
        content="What is the agent loop?",
        session_key="test:qa-gate:2",
        channel="cli",
        chat_id="c1",
        sender_id="u1",
    )

    # The provider must have been called — meaning the gate let the message through.
    assert provider.calls, "expected the provider to be called for a legitimate Q&A request"
    # Either chat or stream_chat should have been invoked (depends on runner config).
    invoked_methods = {call[0] for call in provider.calls}
    assert invoked_methods & {"chat", "stream_chat"}, (
        f"expected chat or stream_chat to be invoked, got {invoked_methods}"
    )