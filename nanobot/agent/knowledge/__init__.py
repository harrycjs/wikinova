"""Knowledge pipeline: IMA → LLM summarization → Obsidian Nanobot/Inbox.

The pipeline owns the "first mile" of the knowledge flow:

1. Pull content from IMA (notes and/or knowledge-base items).
2. For each new item, call an isolated LLM turn to summarize into a
   structured markdown note with YAML frontmatter (title / tags / category /
   source_url / captured_at) + body + key concepts.
3. Atomic-write the summary to ``<vault>/Nanobot/Inbox/<date>-<id>.md``.

The Obsidian sync (Phase C) picks up the new inbox files and turns them
into wiki pages.
"""

from nanobot.agent.knowledge.pipeline import (
    IMAIngestPipeline,
    PipelineResult,
)

__all__ = ["IMAIngestPipeline", "PipelineResult"]
