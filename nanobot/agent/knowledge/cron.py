"""Knowledge sync cron job: IMA → Obsidian → Wiki.

Called every 6 hours via the cron system.  Uses the unified
:class:`IMAIngestPipeline` to fetch new IMA content, summarize via LLM,
and write structured notes to the Obsidian Inbox.  Then scans the Obsidian
vault to regenerate wiki pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger


async def run_knowledge_sync(config: Any, agent: Any) -> None:
    """Full pipeline: IMA → Obsidian → Wiki sync."""
    ima_cfg = getattr(config.tools, "ima", None)
    obs_cfg = getattr(config.tools, "obsidian", None)

    # ── Step 1: IMA sync (fetch new notes + KB items into Obsidian Inbox) ──
    if getattr(ima_cfg, "enabled", False):
        await _sync_ima(config, ima_cfg, obs_cfg, agent)

    # ── Step 2: Obsidian → Wiki sync ──────────────────────────────────────
    if getattr(obs_cfg, "enabled", False) and getattr(obs_cfg, "vault_path", None):
        await _sync_obsidian_to_wiki(config, obs_cfg, agent)

    logger.info("knowledge_sync: completed")


async def _sync_ima(config: Any, ima_cfg: Any, obs_cfg: Any, agent: Any) -> None:
    """Fetch new IMA items → LLM summarize → Obsidian Inbox, via the unified pipeline."""
    try:
        from nanobot.agent.knowledge.pipeline import IMAIngestPipeline
        from nanobot.agent.tools.ima._client import IMAClient

        # Support both camelCase (from config.json) and snake_case
        client_id = getattr(ima_cfg, "client_id", None) or getattr(ima_cfg, "clientId", None)
        api_key = getattr(ima_cfg, "api_key", None) or getattr(ima_cfg, "apiKey", None)
        base_url = getattr(ima_cfg, "base_url", None) or getattr(ima_cfg, "baseUrl", "https://ima.qq.com")

        client = IMAClient.from_env_or_files(
            client_id=client_id,
            api_key=api_key,
            base_url=base_url,
        )
        if not client.has_credentials():
            logger.warning("knowledge_sync: IMA credentials missing, skipping")
            return

        vault_path = getattr(obs_cfg, "vault_path", None) or str(config.workspace_path)
        vault = Path(vault_path).expanduser().resolve()
        vault_root = getattr(ima_cfg, "vault_root", "Nanobot") or "Nanobot"
        inbox_subdir = getattr(ima_cfg, "inbox_subdir", "Inbox") or "Inbox"
        inbox_root = vault / vault_root

        pipeline = IMAIngestPipeline(
            client=client,
            provider=agent.provider,
            model=agent.model,
            inbox_root=inbox_root,
            workspace=Path(config.workspace_path),
            inbox_dir_name=inbox_subdir,
        )

        result = await pipeline.run_all()
        logger.info(
            "knowledge_sync: IMA pipeline done — processed={}, skipped={}, written={}",
            result.items_processed,
            result.items_skipped,
            len(result.notes_written),
        )
        if result.errors:
            for err in result.errors:
                logger.warning("knowledge_sync: IMA error: {}", err)

    except Exception as exc:
        logger.warning("knowledge_sync: IMA sync failed: {}", exc)


async def _sync_obsidian_to_wiki(config: Any, obs_cfg: Any, agent: Any = None) -> None:
    """Scan Obsidian vault → create wiki pages via :class:`ObsidianWikiSync`.

    Uses the canonical sync class (with sha256 diffing and incremental state
    persistence) rather than reimplementing the walk inline. The vault is the
    source of truth; the wiki is just a cache, so we never overwrite existing
    pages whose body hasn't changed.
    """
    try:
        from nanobot.agent.wiki import WikiPaths, WikiStore
        from nanobot.agent.wiki.sync import ObsidianWikiSync

        vault_path = Path(obs_cfg.vault_path).expanduser().resolve()
        if not vault_path.exists():
            logger.warning("knowledge_sync: Obsidian vault not found: {}", vault_path)
            return

        workspace = config.workspace_path
        paths = WikiPaths.from_workspace(Path(workspace))
        store = WikiStore(paths)
        vault_root = getattr(obs_cfg, "nanobot_root", None) or ""
        sync = ObsidianWikiSync(
            store,
            vault_path=vault_path,
            vault_root=vault_root,
        )
        result = await sync.run_once(agent=agent, max_files=50)
        logger.info(
            "knowledge_sync: Obsidian sync — scanned={}, changed={}, generated={}, skipped={}",
            result.scanned,
            len(result.changed),
            len(result.generated),
            len(result.skipped),
        )
        if result.errors:
            for err in result.errors:
                logger.warning("knowledge_sync: Obsidian sync error: {}", err)
    except Exception as exc:
        logger.warning("knowledge_sync: Obsidian sync failed: {}", exc)
