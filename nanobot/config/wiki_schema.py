"""Configuration blocks for the wiki, IMA, and Obsidian integrations.

Imported via ``_resolve_tool_config_refs`` in ``nanobot/config/schema.py`` and
attached to ``ToolsConfig``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from nanobot.config_base import Base


class WikiEvolutionConfig(Base):
    """Periodic self-evolution pass settings."""

    enabled: bool = True
    interval_h: int = Field(default=6, ge=1)
    max_batch_entries: int = Field(default=30, ge=1, le=200)
    max_pages_per_run: int = Field(default=10, ge=1, le=50)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    model_override: str | None = None


class WikiConfig(Base):
    """LLM-generated wiki configuration."""

    enabled: bool = True
    root: str = "wiki"
    max_pages: int = Field(default=1000, ge=1)
    max_page_chars: int = Field(default=32_000, ge=1000)
    search_top_k: int = Field(default=5, ge=1, le=50)
    evolution: WikiEvolutionConfig = Field(default_factory=WikiEvolutionConfig)
    # Importers enabled for the ``POST /api/wiki/import`` endpoint. Empty list
    # disables the endpoint entirely. Order is irrelevant — each importer is
    # keyed by source type and dispatched on demand.
    importers: list[str] = Field(
        default_factory=lambda: ["markdown", "text", "pdf", "url"],
    )


class IMAToolsConfig(Base):
    """Tencent IMA OpenAPI integration (notes + knowledge-base modules)."""

    enabled: bool = False  # opt-in: requires credentials
    client_id: str | None = None
    api_key: str | None = None
    base_url: str = "https://ima.qq.com"
    vault_root: str = "Nanobot"  # sub-path under Obsidian vault
    inbox_subdir: str = "Inbox"
    sync_interval_h: int = Field(default=24, ge=1)
    auto_summarize: bool = True
    timeout_s: float = 30.0

    def has_credentials(self) -> bool:
        return bool(self.client_id and self.api_key)


class ObsidianToolsConfig(Base):
    """Obsidian vault integration (read-only by default)."""

    enabled: bool = False
    mode: Literal["filesystem", "rest_api"] = "filesystem"
    vault_path: str | None = None
    nanobot_root: str = "Nanobot"  # sub-path under vault where IMA summaries land
    rest_api_base: str | None = None
    rest_api_key: str | None = None
    sync_mode: Literal["watch", "poll", "manual"] = "poll"
    poll_interval_s: int = Field(default=60, ge=5)
    sync_on_startup: bool = True
