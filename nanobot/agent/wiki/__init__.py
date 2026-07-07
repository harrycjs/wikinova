"""Wiki subsystem for nanobot.

Public surface:

- :class:`WikiPaths` — on-disk layout.
- :class:`WikiStore` — durable storage with atomic writes + git audit.
- :class:`WikiQuerier` — BM25 search over wiki pages.
- :class:`WikiGenerator` — drives isolated agent turns to author pages.
- :class:`WikiEvolution` — periodic self-evolution loop.
- :func:`build_wiki_tool_registry` — builds ToolRegistry for agent turns.
"""

from nanobot.agent.wiki.evolution import EvolutionRunResult, WikiEvolution
from nanobot.agent.wiki.generator import GenerationResult, WikiGenerator
from nanobot.agent.wiki.paths import WikiPaths
from nanobot.agent.wiki.querier import Hit, WikiQuerier
from nanobot.agent.wiki.store import WikiPage, WikiStore
from nanobot.agent.wiki.tools import build_wiki_tool_registry

__all__ = [
    "WikiPaths",
    "WikiStore",
    "WikiPage",
    "WikiQuerier",
    "Hit",
    "WikiGenerator",
    "GenerationResult",
    "WikiEvolution",
    "EvolutionRunResult",
    "build_wiki_tool_registry",
]
