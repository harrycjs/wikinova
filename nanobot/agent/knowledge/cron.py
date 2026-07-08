"""Knowledge sync cron job: IMA → Obsidian → Wiki.

Called every 6 hours via the cron system.  Uses the unified
:class:`IMAIngestPipeline` to fetch new IMA content, summarize via LLM,
and write structured notes to the Obsidian Inbox.  Then scans the Obsidian
vault to regenerate wiki pages.
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timezone
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
        await _sync_obsidian_to_wiki(config, obs_cfg)

    logger.info("knowledge_sync: completed")


async def _sync_ima(config: Any, ima_cfg: Any, obs_cfg: Any, agent: Any) -> None:
    """Fetch new IMA items → LLM summarize → Obsidian Inbox, via the unified pipeline."""
    try:
        from nanobot.agent.knowledge.pipeline import IMAIngestPipeline
        from nanobot.agent.tools.ima._client import IMAClient

        client = IMAClient.from_env_or_files(
            client_id=getattr(ima_cfg, "client_id", None),
            api_key=getattr(ima_cfg, "api_key", None),
            base_url=getattr(ima_cfg, "base_url", "https://ima.qq.com"),
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


async def _sync_obsidian_to_wiki(config: Any, obs_cfg: Any) -> None:
    """Scan Obsidian vault → create wiki pages."""
    try:
        vault_path = Path(obs_cfg.vault_path).expanduser().resolve()
        if not vault_path.exists():
            logger.warning("knowledge_sync: Obsidian vault not found: {}", vault_path)
            return

        from nanobot.agent.wiki import WikiPaths, WikiStore
        from nanobot.agent.wiki.frontmatter import parse_frontmatter

        import hashlib

        workspace = config.workspace_path
        paths = WikiPaths.from_workspace(Path(workspace))
        store = WikiStore(paths)
        md_files = sorted(vault_path.rglob("*.md"))
        created = 0
        for md_file in md_files[:30]:
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not content.strip():
                continue

            fm, body = parse_frontmatter(content)
            title = fm.title or md_file.stem
            stem = md_file.stem
            slug = _re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).lower().replace(" ", "-")
            slug = _re.sub(r"[^a-z0-9-]", "", slug).strip("-")
            if not slug or len(slug) < 3:
                slug = f"vault-{hashlib.md5(md_file.name.encode()).hexdigest()[:8]}"
            slug = slug[:80]

            body_content = body if body else content
            try:
                store.write_page(
                    slug=slug,
                    title=title,
                    body=body_content[:8000],
                    tags=["obsidian", "vault"],
                    source=f"obsidian:{md_file.relative_to(vault_path).as_posix()}",
                )
                created += 1
            except Exception:
                pass
        logger.info("knowledge_sync: Obsidian synced {} pages to wiki", created)
    except Exception as exc:
        logger.warning("knowledge_sync: Obsidian sync failed: {}", exc)
