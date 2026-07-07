"""Pre-LLM Q&A intent gate.

Forces the agent to refuse off-topic requests before consuming tokens on a full
agent turn. When ``AgentDefaults.qa_mode`` is enabled, every inbound message is
classified into one of:

- ``factual_q``         — a knowledge-base question, proceed normally.
- ``concept_explain``   — "what is X / explain Y", proceed normally.
- ``search``            — explicit search/list request, proceed normally.
- ``kb_management``     — sync/regenerate/import commands, proceed normally.
- ``off_topic``         — refuse with the canned rejection message.

The classifier is intentionally cheap: a keyword/pattern scan first, falling
back to a tiny LLM call only when confidence is low. Off-topic decisions are
final — no LLM turn is run, no tools are invoked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


QA_INTENT_FACTUAL = "factual_q"
QA_INTENT_EXPLAIN = "concept_explain"
QA_INTENT_SEARCH = "search"
QA_INTENT_KB_MGMT = "kb_management"
QA_INTENT_OFF_TOPIC = "off_topic"

ALL_QA_INTENTS = frozenset(
    {
        QA_INTENT_FACTUAL,
        QA_INTENT_EXPLAIN,
        QA_INTENT_SEARCH,
        QA_INTENT_KB_MGMT,
        QA_INTENT_OFF_TOPIC,
    }
)


@dataclass(frozen=True)
class QAGateDecision:
    intent: str
    proceed: bool
    refusal: str | None
    confidence: float


# Chinese + English patterns that strongly indicate a knowledge-base question.
_QA_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Chinese
    re.compile(r"知识库里|知识库有|笔记里|笔记有|我(的)?(笔记|知识库)|之前.*记", re.IGNORECASE),
    re.compile(r"什么是|啥是|解释|介绍一下|总结一下|归纳|整理|梳理"),
    re.compile(r"有没有关于|有没有.*笔记|查.*笔记|搜.*知识库|帮我找|帮我查"),
    re.compile(r"关于.{0,20}的(笔记|资料|记录|总结|介绍)"),
    # English
    re.compile(r"\bwhat(?:'s| is| are)\b", re.IGNORECASE),
    re.compile(r"\b(explain|describe|summari[sz]e|tell me about|look up|find|search)\b", re.IGNORECASE),
    re.compile(r"\bin (my|our|the) (notes?|wiki|knowledge base|vault)\b", re.IGNORECASE),
    re.compile(r"\b(do (i|we) have|have i|is there).{0,40}(notes?|wiki|knowledge)", re.IGNORECASE),
)

# KB-management commands — sync, regenerate, refresh.
_KB_MGMT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsync\b", re.IGNORECASE),
    re.compile(r"\b(regenerate|refresh|rebuild|reindex|import)\b", re.IGNORECASE),
    re.compile(r"\bupdate\b\s+(the\s+)?(wiki|knowledge|obsidian|vault|ima)", re.IGNORECASE),
    re.compile(r"(同步|刷新|重新生成|更新|导入|重建|重建).{0,12}(知识库|笔记|wiki|obsidian|ima|im)"),
    re.compile(r"(从|从.{0,4})(ima|obsidian|知识库).{0,12}(同步|拉取|导入|抓取|获取)"),
    re.compile(r"(ima|obsidian|vault|wiki|知识库|笔记).{0,12}(同步|sync|刷新|refresh|拉取|更新)"),
)

# Patterns that strongly indicate off-topic work.
_OFF_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Code generation / debugging
    re.compile(r"\b(write|implement|create|build|fix|debug|refactor|review|develop)\s+(a\s+|an\s+)?(function|class|script|program|app|api|component|module|file|method|algorithm|server|service|binary search|quicksort)", re.IGNORECASE),
    re.compile(r"\b(write|implement)\s+(a\s+|an\s+)?(binary search|quicksort|hashtable|linked\s?list|recursive)", re.IGNORECASE),
    re.compile(r"帮我(写|改|修|调试|实现|开发).{0,15}(代码|函数|脚本|程序|class|python|javascript|typescript|rust|go)"),
    re.compile(r"\b(python|javascript|typescript|rust|java|c\+\+|c#|go|ruby|php|swift|kotlin)\s+(code|script|function|class|implementation)\b", re.IGNORECASE),
    re.compile(r"\bbinary search\b", re.IGNORECASE),
    # Creative writing
    re.compile(r"^(write|compose|craft|draft)\s+(a\s+|an\s+)?(poem|story|song|essay|joke|script|dialogue|haiku|limerick|ballad|sonnet)", re.IGNORECASE),
    re.compile(r"^(写|创作|作|来).{0,8}(诗|故事|小说|剧本|歌词|段子|笑话|rap|绕口令|散文|情书)"),
    re.compile(r"写(一首|一篇|一段|个|了|个)?(诗|故事|小说|剧本|歌词|段子|笑话|rap|绕口令)"),
    # Math / calculations
    re.compile(r"^(calculate|compute|solve|evaluate|simplify|factor|integrate|differentiate)\b", re.IGNORECASE),
    re.compile(r"(等于多少|算一下|计算|求解|求[积分导数])"),
    # General chitchat — only block very short greetings with no real content
    # "你好" alone is fine — let the LLM handle it via the knowledge-qa skill
    re.compile(r"^(thanks|thank you|谢谢|thx)\s*[!.,]*\s*$", re.IGNORECASE),
    # Tool execution outside KB
    re.compile(r"^(run|execute|open|launch|start|deploy)\s+(a\s+|an\s+|the\s+)?(command|shell|terminal|server|docker|container|nginx|redis|postgres|kubernetes)", re.IGNORECASE),
    re.compile(r"(执行|运行|打开|启动|跑|部署).{0,12}(命令|终端|server|docker|容器|程序)"),
)

_REFUSAL_MESSAGE = (
    "我是知识库问答助手，只能基于你的私有知识库回答问题。你可以问我知识库里的内容，"
    "或者让我帮你搜索相关的笔记。试试问我「知识库里有什么？」"
)


def _strip_for_match(text: str) -> str:
    """Lowercase + collapse whitespace for pattern matching."""
    return re.sub(r"\s+", " ", text.strip())


def quick_classify(text: str) -> QAGateDecision:
    """Fast pattern-based classifier; no LLM call.

    Returns a :class:`QAGateDecision` whose ``proceed`` field is False only for
    clearly off-topic requests. Low-confidence cases are allowed through (the
    LLM itself will see the Identity template and refuse).
    """
    if not text or not text.strip():
        # Empty inbound — let the existing handlers deal with it.
        return QAGateDecision(
            intent=QA_INTENT_FACTUAL,
            proceed=True,
            refusal=None,
            confidence=1.0,
        )

    stripped = _strip_for_match(text)

    # 1) KB management commands first — they're explicitly allowed.
    for pat in _KB_MGMT_PATTERNS:
        if pat.search(stripped):
            return QAGateDecision(
                intent=QA_INTENT_KB_MGMT,
                proceed=True,
                refusal=None,
                confidence=0.9,
            )

    # 2) Strong off-topic signals → refuse without an LLM call.
    for pat in _OFF_TOPIC_PATTERNS:
        if pat.search(stripped):
            return QAGateDecision(
                intent=QA_INTENT_OFF_TOPIC,
                proceed=False,
                refusal=_REFUSAL_MESSAGE,
                confidence=0.85,
            )

    # 3) Q&A patterns → allow.
    for pat in _QA_PATTERNS:
        if pat.search(stripped):
            return QAGateDecision(
                intent=QA_INTENT_FACTUAL,
                proceed=True,
                refusal=None,
                confidence=0.85,
            )

    # 4) Short greetings/social messages → refuse softly but still refuse.
    if len(stripped) <= 30 and re.match(r"^(hi|hello|hey|你好|在吗|thanks|thank you|谢谢)\b", stripped, re.IGNORECASE):
        return QAGateDecision(
            intent=QA_INTENT_OFF_TOPIC,
            proceed=False,
            refusal=_REFUSAL_MESSAGE,
            confidence=0.6,
        )

    # 5) Unknown — allow through and let the LLM (with Identity + skill) decide.
    return QAGateDecision(
        intent=QA_INTENT_FACTUAL,
        proceed=True,
        refusal=None,
        confidence=0.4,
    )


async def llm_classify(provider: "LLMProvider", text: str, model: str) -> QAGateDecision:
    """Optional LLM-based classifier for low-confidence cases.

    Currently unused by default (see ``quick_classify``); wired in for future
    upgrade where ambiguous messages get a single-shot classification call.
    """
    from nanobot.providers.base import GenerationSettings

    system = (
        "You are a binary intent classifier for a knowledge-base Q&A assistant. "
        "Given a user message, respond with ONLY one of these tokens:\n"
        "- QA  — if the message asks about topics, definitions, summaries, or anything answerable from the user's notes/wiki/vault.\n"
        "- KB  — if the message is a command to sync, regenerate, refresh, or otherwise manage the knowledge base.\n"
        "- OFF — if the message is general chitchat, code generation, creative writing, math, or anything outside knowledge-base Q&A.\n"
        "Respond with exactly one token, nothing else."
    )
    prompt = f"User message: {text!r}\n\nClassification:"
    settings = GenerationSettings(max_tokens=4, temperature=0.0)
    try:
        response = await provider.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=model,
            settings=settings,
            tools=None,
        )
        token = (response.content or "").strip().upper()
    except Exception:  # noqa: BLE001 — never let classification break a turn.
        return QAGateDecision(
            intent=QA_INTENT_FACTUAL,
            proceed=True,
            refusal=None,
            confidence=0.0,
        )

    if token == "OFF":
        return QAGateDecision(
            intent=QA_INTENT_OFF_TOPIC,
            proceed=False,
            refusal=_REFUSAL_MESSAGE,
            confidence=0.95,
        )
    if token == "KB":
        return QAGateDecision(
            intent=QA_INTENT_KB_MGMT,
            proceed=True,
            refusal=None,
            confidence=0.95,
        )
    return QAGateDecision(
        intent=QA_INTENT_FACTUAL,
        proceed=True,
        refusal=None,
        confidence=0.95,
    )
