"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.command.builtin import builtin_command_palette
from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import CronJob, CronSchedule
from nanobot.triggers.local_types import LocalTrigger
from nanobot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanobot.webui.file_preview import WebUIFilePreviewError, file_preview_payload
from nanobot.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from nanobot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from nanobot.webui.http_utils import (
    host_for_url as _host_for_url,
)
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)
from nanobot.webui.http_utils import (
    is_localhost as _is_localhost,
)
from nanobot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.session_automations import (
    all_automations_payload,
    serialize_automation_jobs,
    session_automation_jobs,
    session_automations_payload,
)
from nanobot.webui.session_list_index import list_webui_sessions
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.skills_api import webui_skill_detail_payload, webui_skills_payload
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import build_webui_thread_response
from nanobot.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_AUTOMATION_VALUES_HEADER = "X-Nanobot-Automation-Values"

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager
    from nanobot.triggers.local_store import LocalTriggerStore


def _decode_api_key(raw_key: str) -> str | None:
    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


def _default_model_name_from_config() -> str | None:
    try:
        from nanobot.config.loader import load_config
        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config() or ""


# ---------------------------------------------------------------------------
# GatewayHTTPHandler
# ---------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: Any,  # WebSocketConfig
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUIMediaGateway,
        workspaces: WebUIWorkspaceController,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        cron_service: CronService | None = None,
        local_trigger_store: LocalTriggerStore | None = None,
        cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        local_trigger_pending_ids: Callable[[str], set[str]] | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.workspaces = workspaces
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills = disabled_skills or set()
        self.cron_service = cron_service
        self.local_trigger_store = local_trigger_store
        self.cron_pending_job_ids = cron_pending_job_ids
        self.local_trigger_pending_ids = local_trigger_pending_ids
        self._log = log
        self._runtime_surface = runtime_surface

        # In-memory registry for in-flight wiki-evolve tasks. Maps task_id → dict
        # with keys: status ("queued"|"running"|"done"|"failed"), started_at,
        # finished_at, summary, error. Lost on restart, but that's acceptable:
        # the next evolution tick will catch any missed work and the wiki log
        # (``WikiEvolution.read_recent_log``) is the persistent record.
        self._regenerate_tasks: dict[str, dict[str, Any]] = {}

        from nanobot.webui.settings_api import runtime_capabilities as _rc
        from nanobot.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
        )

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    # -- Token management ---------------------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None

        try:
            response = await self._dispatch_resolved(connection, request, got)
            return response
        finally:
            self._log_slow_http(got, response, started)

    async def _dispatch_resolved(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
    ) -> Any | None:
        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(connection, request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Automation routes
        response = await self._dispatch_automation_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = await self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _log_slow_http(self, path: str, response: Any | None, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms < _SLOW_WEBUI_HTTP_LOG_MS:
            return
        if not (path.startswith("/api/") or path == "/webui/bootstrap"):
            return
        status = getattr(response, "status_code", None)
        self._log.warning(
            "slow webui http route path={} status={} duration_ms={}",
            path,
            status if status is not None else "none",
            elapsed_ms,
        )

    # -- Token issue --------------------------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self._log.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(token_response_payload(token_value, self.config.token_ttl_s))

    # -- Bootstrap ----------------------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            return _http_error(403, "bootstrap is localhost-only")

        if not self.tokens.can_issue(include_api_token=True):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = self.tokens.issue_token(self.config.token_ttl_s, api_token=True)

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        return _http_json_response(
            {
                "token": token,
                "ws_path": expected_path,
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    # -- Session routes -----------------------------------------------------

    async def _dispatch_session_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/file-preview$", got)
        if m:
            return self._handle_file_preview(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/automations$", got)
        if m:
            return self._handle_session_automations(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        return None

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        payload = await asyncio.to_thread(self._sessions_list_payload)
        return _http_json_response(payload)

    def _sessions_list_payload(self) -> dict[str, Any]:
        assert self.session_manager is not None
        sessions = list_webui_sessions(self.session_manager)
        from nanobot.session.webui_turns import websocket_turn_wall_started_at

        cleaned = []
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith("websocket:")):
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            chat_id = key.split(":", 1)[1]
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            scope = self.workspaces.scope_for_session_key(key)
            row["workspace_scope"] = scope.payload()
            cleaned.append(row)
        return {"sessions": cleaned}

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self.session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        self.media.augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        session_messages: list[dict[str, Any]] | None = None
        if self.session_manager is not None:
            session_data = self.session_manager.read_session_file(decoded_key)
            raw_messages = session_data.get("messages") if isinstance(session_data, dict) else None
            if isinstance(raw_messages, list):
                session_messages = [m for m in raw_messages if isinstance(m, dict)]
        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        limit: int | None = None
        if raw_limit is not None and raw_limit.strip():
            try:
                limit = int(raw_limit)
            except ValueError:
                return _http_error(400, "invalid limit")
        direction = _query_first(query, "direction")
        if direction is not None and direction not in {"latest"}:
            return _http_error(400, "invalid direction")
        before = _query_first(query, "before")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
            session_messages=session_messages,
            limit=limit,
            direction=direction,
            before=before,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        data["workspace_scope"] = scope.payload()
        return _http_json_response(data)

    def _handle_file_preview(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        path = _query_first(_parse_query(request.path), "path")
        try:
            payload = file_preview_payload(
                path,
                scope=self.workspaces.scope_for_session_key(decoded_key),
            )
        except WebUIFilePreviewError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_session_automations(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        pending_job_ids = self._pending_automation_ids_for_session(decoded_key)
        return _http_json_response(
            session_automations_payload(
                self.cron_service,
                decoded_key,
                local_trigger_store=self.local_trigger_store,
                pending_job_ids=pending_job_ids,
            )
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        query = _parse_query(request.path)
        delete_automations = (_query_first(query, "delete_automations") or "").lower()
        automation_jobs = session_automation_jobs(
            self.cron_service,
            decoded_key,
            local_trigger_store=self.local_trigger_store,
        )
        if automation_jobs and delete_automations not in {"1", "true", "yes"}:
            return _http_json_response(
                {
                    "deleted": False,
                    "blocked_by_automations": True,
                    "automations": serialize_automation_jobs(automation_jobs),
                }
            )
        if automation_jobs:
            for job in automation_jobs:
                if isinstance(job, LocalTrigger):
                    if self.local_trigger_store is not None:
                        self.local_trigger_store.delete(job.id)
                elif self.cron_service is not None:
                    self.cron_service.remove_job(job.id)
        deleted = self.session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    # -- Automation routes --------------------------------------------------

    async def _dispatch_automation_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if got == "/api/webui/automations":
            return self._handle_webui_automations(request)
        m = re.match(r"^/api/webui/automations/(enable|disable|delete|run|update)$", got)
        if m:
            return await self._handle_webui_automation_action(request, m.group(1))
        return None

    def _pending_cron_job_ids_for_all(self) -> set[str]:
        if self.cron_service is None or self.cron_pending_job_ids is None:
            return set()
        pending: set[str] = set()
        for job in self.cron_service.list_jobs(include_disabled=True):
            session_key = job.payload.session_key
            if not session_key and job.payload.origin_channel and job.payload.origin_chat_id:
                session_key = f"{job.payload.origin_channel}:{job.payload.origin_chat_id}"
            if session_key:
                pending.update(self.cron_pending_job_ids(session_key))
        return pending

    def _pending_local_trigger_ids_for_all(self) -> set[str]:
        if self.local_trigger_store is None or self.local_trigger_pending_ids is None:
            return set()
        pending: set[str] = set()
        for trigger in self.local_trigger_store.list_triggers(include_disabled=True):
            session_key = trigger.session_key
            if not session_key and trigger.channel and trigger.chat_id:
                session_key = f"{trigger.channel}:{trigger.chat_id}"
            if session_key:
                pending.update(self.local_trigger_pending_ids(session_key))
        return pending

    def _pending_automation_ids_for_session(self, session_key: str) -> set[str]:
        pending: set[str] = set()
        if self.cron_pending_job_ids is not None:
            pending.update(self.cron_pending_job_ids(session_key))
        if self.local_trigger_pending_ids is not None:
            pending.update(self.local_trigger_pending_ids(session_key))
        return pending

    def _handle_webui_automations(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        pending_job_ids = self._pending_cron_job_ids_for_all()
        pending_job_ids.update(self._pending_local_trigger_ids_for_all())
        return _http_json_response(
            all_automations_payload(
                self.cron_service,
                local_trigger_store=self.local_trigger_store,
                session_manager=self.session_manager,
                pending_job_ids=pending_job_ids,
            )
        )

    async def _handle_webui_automation_action(
        self,
        request: WsRequest,
        action: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.cron_service is None and self.local_trigger_store is None:
            return _http_error(503, "automation service unavailable")

        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or _query_first(query, "job_id") or "").strip()
        if not job_id:
            return _http_error(400, "missing automation id")
        trigger = self.local_trigger_store.get(job_id) if self.local_trigger_store else None
        if trigger is not None:
            return self._handle_local_trigger_action(request, action, trigger)

        if self.cron_service is None:
            return _http_error(404, "automation not found")
        job = self.cron_service.get_job(job_id)
        if job is None:
            return _http_error(404, "automation not found")
        if job.payload.kind == "system_event":
            return _http_error(403, "system automation is protected")
        if action in {"enable", "run"} and not is_bound_cron_job(job):
            return _http_error(409, "automation has no linked chat")

        if action == "enable":
            if self.cron_service.enable_job(job_id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.cron_service.enable_job(job_id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            result = self.cron_service.remove_job(job_id)
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        elif action == "run":
            if not job.enabled:
                return _http_error(409, "automation is disabled")
            task = asyncio.create_task(self.cron_service.run_job(job_id, force=False))
            task.add_done_callback(self._log_automation_run_result)
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_automation_update(values, current_job=job)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            try:
                result = self.cron_service.update_job(job_id, **parsed)
            except ValueError as exc:
                return _http_error(400, str(exc))
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    def _handle_local_trigger_action(
        self,
        request: WsRequest,
        action: str,
        trigger: LocalTrigger,
    ) -> Response:
        if self.local_trigger_store is None:
            return _http_error(503, "trigger service unavailable")
        if action == "enable":
            if self.local_trigger_store.enable(trigger.id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.local_trigger_store.enable(trigger.id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            if not self.local_trigger_store.delete(trigger.id):
                return _http_error(404, "automation not found")
        elif action == "run":
            return _http_error(409, "local trigger requires a CLI message")
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_local_trigger_update(values)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            if parsed:
                if self.local_trigger_store.update(trigger.id, **parsed) is None:
                    return _http_error(404, "automation not found")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    @staticmethod
    def _log_automation_run_result(task: asyncio.Task[bool]) -> None:
        try:
            ran = task.result()
        except Exception:
            logger.exception("WebUI automation run-now task failed")
            return
        if not ran:
            logger.warning("WebUI automation run-now task did not execute")

    # -- Media routes -------------------------------------------------------

    def _dispatch_media_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2), request)
        return None

    def _handle_media_fetch(
        self, sig: str, payload: str, request: WsRequest | None = None
    ) -> Response:
        return self.media.serve_signed_media(
            sig,
            payload,
            request=request,
        )

    # -- Misc routes --------------------------------------------------------

    async def _dispatch_misc_routes(
        self, connection: Any, request: WsRequest, got: str
    ) -> Response | None:
        if got == "/api/sessions":
            return await self._handle_sessions_list(request)
        if got == "/api/commands":
            return self._handle_commands(request)
        if got == "/api/workspaces":
            return self._handle_workspaces(connection, request)
        if got == "/api/webui/skills":
            return self._handle_webui_skills(request)
        # Wiki / IMA / Obsidian / Plugins endpoints
        if got == "/api/wiki/list":
            return self._handle_wiki_list(request)
        if got == "/api/wiki/evolution":
            return self._handle_wiki_evolution(request)
        if got == "/api/wiki/page":
            query = _parse_query(request.path)
            slug = _query_first(query, "slug") or ""
            if slug:
                return self._handle_wiki_page(request, slug)
        if got == "/api/wiki/search":
            return self._handle_wiki_search(request)
        if got == "/api/wiki/regenerate":
            return await self._handle_wiki_regenerate(request)
        if got == "/api/wiki/regenerate/status":
            return self._handle_wiki_regenerate_status(request)
        if got == "/api/wiki/edit":
            return self._handle_wiki_edit(request)
        if got == "/api/wiki/import":
            return self._handle_wiki_import(request)
        if got == "/api/wiki/delete":
            return self._handle_wiki_delete(request)
        if got == "/api/wiki/lint":
            return await self._handle_wiki_lint(request)
        if got == "/api/wiki/regenerate":
            return await self._handle_wiki_regenerate(request)
        if got == "/api/ima/status":
            return self._handle_ima_status(request)
        if got == "/api/ima/sync":
            return await self._handle_ima_sync(request)
        if got == "/api/obsidian/status":
            return self._handle_obsidian_status(request)
        if got == "/api/obsidian/resync":
            return self._handle_obsidian_resync(request)
        if got == "/api/plugins/list":
            return self._handle_plugins_list(request)
        if got == "/api/channels/save-config":
            return self._handle_channels_save_config(request)
        if got == "/api/channels/list":
            return self._handle_channels_list(request)
        if got == "/api/channels/config":
            return self._handle_channels_config(request)
        if got == "/api/weixin/qrcode":
            return self._handle_weixin_qrcode(request)
        if got == "/api/weixin/status":
            return self._handle_weixin_status(request)
        if got == "/api/wxmp/status":
            return self._handle_wxmp_status(request)
        if got == "/api/wxmp/login":
            return await self._handle_wxmp_login(request)
        if got == "/api/wxmp/logout":
            return self._handle_wxmp_logout(request)
        m = re.match(r"^/api/webui/skills/([^/]+)$", got)
        if m:
            return self._handle_webui_skill_detail(request, m.group(1))
        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)
        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)
        return None

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_workspaces(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            self.workspaces.payload(
                controls_available=self.workspace_controls_available(connection)
            )
        )

    def _handle_webui_skills(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            )
        )

    def _handle_webui_skill_detail(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        name = unquote(raw_name)
        if not name or "/" in name or "\\" in name:
            return _http_error(400, "invalid skill name")
        payload = webui_skill_detail_payload(
            self.skills_workspace_path,
            name,
            disabled_skills=self.disabled_skills,
        )
        if payload is None:
            return _http_error(404, "skill not found")
        return _http_json_response(payload)

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded)
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self._log.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    # -- Wiki / IMA / Obsidian / Plugins routes -----------------------------

    def _handle_wiki_list(self, request: WsRequest) -> Response:
        from nanobot.agent.wiki import WikiPaths, WikiStore

        workspace = Path(self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace").expanduser().resolve()
        try:
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            pages = store.list_pages()
            return _http_json_response(pages)
        except Exception as e:
            return _http_error(500, str(e))

    def _handle_wiki_evolution(self, request: WsRequest) -> Response:
        from nanobot.agent.wiki import WikiPaths, WikiStore

        workspace = Path(self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace").expanduser().resolve()
        try:
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            from nanobot.agent.wiki.evolution import WikiEvolution

            evo = WikiEvolution(store, None)  # type: ignore[arg-type]
            log = evo.read_recent_log()
            return _http_json_response(log)
        except Exception as e:
            return _http_error(500, str(e))

    def _handle_wiki_page(self, request: WsRequest, slug: str) -> Response:
        from nanobot.agent.wiki import WikiPaths, WikiStore

        workspace = Path(self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace").expanduser().resolve()
        try:
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            page = store.read_page(slug)
            if page is None:
                return _http_error(404, "Page not found")
            blinks = store.backlinks(slug)
            data = page.to_dict()
            data["body"] = page.body
            data["frontmatter"] = page.fm.to_dict()
            data["backlinks"] = blinks
            # Surface the page's on-disk byte size so the WebUI can detect if
            # the stored body was clipped at ``WikiConfig.max_page_chars`` and
            # warn the user. We approximate the original length as the file's
            # raw byte count (minus the frontmatter) — the store does not
            # preserve the exact pre-truncation length.
            try:
                page_path = paths.page_path(slug)
                if page_path.exists():
                    data["stored_length"] = page_path.stat().st_size
            except OSError:
                pass
            return _http_json_response(data)
        except Exception as e:
            return _http_error(500, str(e))

    def _handle_wiki_search(self, request: WsRequest) -> Response:
        from nanobot.agent.wiki import WikiPaths, WikiQuerier, WikiStore

        query = _parse_query(request.path)
        q = _query_first(query, "q") or ""
        workspace = Path(self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace").expanduser().resolve()
        try:
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            querier = WikiQuerier(store)
            hits = querier.search(q, k=5)
            return _http_json_response([h.to_dict() for h in hits])
        except Exception as e:
            return _http_error(500, str(e))

    async def _handle_wiki_regenerate(self, request: WsRequest) -> Response:
        """Trigger the wiki-evolve cron job in the background.

        Returns immediately with a ``task_id`` that the frontend can poll via
        ``/api/wiki/regenerate/status?task_id=...``. The actual evolution runs
        asynchronously through the cron service's existing job handler, which
        has access to the ``AgentLoop`` for the isolated LLM turn.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        if self.cron_service is None:
            return _http_error(503, "cron service unavailable")

        # Verify the job is actually registered; refuse early if not so the
        # client gets a clean 404 instead of a stuck "running" task.
        job = self.cron_service.get_job("wiki_evolve")
        if job is None:
            return _http_error(
                404,
                "wiki_evolve job is not registered (enable tools.wiki.evolution in config)",
            )

        task_id = uuid.uuid4().hex
        self._regenerate_tasks[task_id] = {
            "status": "queued",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "summary": None,
            "error": None,
        }

        async def _runner() -> None:
            self._regenerate_tasks[task_id]["status"] = "running"
            try:
                ok = await self.cron_service.run_job("wiki_evolve", force=True)
                if ok:
                    self._regenerate_tasks[task_id]["status"] = "done"
                    self._regenerate_tasks[task_id]["summary"] = (
                        "evolution pass completed; see /api/wiki/evolution for the new log"
                    )
                else:
                    # run_job returns False when the job is disabled or unbound.
                    self._regenerate_tasks[task_id]["status"] = "failed"
                    self._regenerate_tasks[task_id]["error"] = (
                        "wiki_evolve job could not run (disabled or unbound)"
                    )
            except Exception as exc:  # noqa: BLE001
                self._regenerate_tasks[task_id]["status"] = "failed"
                self._regenerate_tasks[task_id]["error"] = str(exc)
                self._log.exception("wiki regenerate task {} failed", task_id)
            finally:
                self._regenerate_tasks[task_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Fire-and-forget. The HTTP response goes back to the client immediately
        # while evolution runs in the gateway's event loop.
        asyncio.create_task(_runner())

        return _http_json_response(
            {"ok": True, "task_id": task_id, "status": "queued"}
        )

    def _handle_wiki_regenerate_status(self, request: WsRequest) -> Response:
        """Poll the status of an in-flight wiki regenerate task."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        task_id = _query_first(query, "task_id") or ""
        if not task_id:
            return _http_error(400, "task_id is required")
        record = self._regenerate_tasks.get(task_id)
        if record is None:
            return _http_error(404, f"unknown task_id: {task_id}")
        return _http_json_response({"task_id": task_id, **record})

    async def _handle_wiki_import(self, request: WsRequest) -> Response:
        """Import a document into the wiki.

        Accepts either:
        - JSON body ``{"type": "url", "url": "https://...", "title": "..."}``
          to fetch a remote page and extract its text.
        - JSON body ``{"type": "text" | "markdown", "title": "...", "body": "..."}``
          for direct paste / drag-drop of small text snippets.
        - Multipart form-data with a single ``file`` field for uploaded files.
          The ``.md`` / ``.txt`` extensions pass through verbatim; ``.pdf``
          requires an optional ``pypdf`` dependency (best-effort fallback
          returns a 501 if the library is missing).

        The new page is written via ``WikiStore.write_page`` with
        ``merge_existing=False`` so callers get a fresh slug/title.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        content_type = (request.headers.get("content-type") or "").lower()
        logger.info("wiki/import: content_type={}, body_len={}", content_type, len(request.body) if request.body else 0)

        try:
            from nanobot.agent.wiki import WikiPaths, WikiStore

            workspace = Path(
                self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace"
            ).expanduser().resolve()
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)

            if content_type.startswith("multipart/form-data"):
                logger.info("wiki/import: parsing multipart form data")
                body, title, error = await self._read_multipart_file(request)
                logger.info("wiki/import: after parse body_len={}, title={}, error={}", len(body) if body else 0, title, error)
            else:
                try:
                    payload = json.loads(request.body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return _http_error(400, f"invalid JSON body: {exc}")
                body, title, error = await self._resolve_import_payload(payload)
            if error:
                logger.warning("wiki/import: error={}", error)
                return _http_error(400, error)
            if not body or not body.strip():
                return _http_error(400, "imported content is empty")

            logger.info("wiki/import: title={}, body_len={}", title, len(body) if body else 0)
            slug_source = title or body.splitlines()[0][:80]
            slug = self._slugify(slug_source) or f"import-{uuid.uuid4().hex[:8]}"
            logger.info("wiki/import: slug={}", slug)
            # Avoid clobbering an existing page — append a short suffix when
            # the desired slug is taken.
            if store.read_page(slug) is not None:
                slug = f"{slug}-{uuid.uuid4().hex[:6]}"

            page = store.write_page(
                slug=slug,
                title=title or slug.replace("-", " ").title(),
                body=body,
                tags=["imported"],
                source=f"webui-import:{content_type or 'unknown'}",
                merge_existing=False,
            )
            logger.info("wiki/import: success slug={}", page.slug)
            return _http_json_response(
                {"ok": True, "slug": page.slug, "title": page.title, "chars": len(body)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("wiki/import: failed")
            return _http_error(500, str(exc))

    @staticmethod
    def _slugify(text: str) -> str:
        import re as _re

        slug = _re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
        slug = _re.sub(r"-+", "-", slug)[:80].strip("-")
        return slug

    async def _read_multipart_file(self, request: WsRequest):
        """Best-effort multipart parser. Returns (body, title, error)."""
        # We don't pull in a multipart library just for this endpoint — the
        # WebUI only sends ``file=@<path>`` with an optional ``title`` field.
        # Parsing handles a single file part with utf-8 content.
        raw = request.body
        ctype = request.headers.get("content-type") or ""
        boundary = None
        for token in ctype.split(";"):
            token = token.strip()
            if token.startswith("boundary="):
                boundary = token.split("=", 1)[1].strip('"')
                break
        if not boundary:
            return None, None, "multipart boundary missing"
        sep = ("--" + boundary).encode()
        parts = raw.split(sep)
        body = None
        title = None
        for part in parts:
            if not part or part == b"--\r\n" or part == b"--":
                continue
            # Strip leading \r\n if present
            if part.startswith(b"\r\n"):
                part = part[2:]
            head, _, payload = part.partition(b"\r\n\r\n")
            if not head:
                continue
            head_str = head.decode("utf-8", errors="replace")
            disposition = ""
            for line in head_str.splitlines():
                if line.lower().startswith("content-disposition:"):
                    disposition = line
                    break
            name = None
            filename = None
            for chunk in disposition.split(";"):
                chunk = chunk.strip()
                if chunk.startswith("name="):
                    name = chunk.split("=", 1)[1].strip('"')
                elif chunk.startswith("filename="):
                    filename = chunk.split("=", 1)[1].strip('"')
            # Strip trailing \r\n if present
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if name == "title":
                title = payload.decode("utf-8", errors="replace")
            elif name == "file" and filename:
                lower = filename.lower()
                if lower.endswith(".pdf"):
                    extracted = self._extract_pdf_text(payload)
                    if extracted is None:
                        return None, None, "PDF import requires the optional pypdf dependency"
                    body = extracted
                    if not title:
                        title = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                else:
                    body = payload.decode("utf-8", errors="replace")
                    if not title:
                        title = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if body is None:
            return None, None, "no file part found"
        return body, title, None

    @staticmethod
    def _extract_pdf_text(payload: bytes) -> str | None:
        try:
            import pypdf  # type: ignore
        except ImportError:
            return None
        try:
            reader = pypdf.PdfReader(io.BytesIO(payload))
        except Exception:  # noqa: BLE001
            return None
        chunks: list[str] = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        return "\n\n".join(chunks).strip()

    @staticmethod
    async def _resolve_import_payload(payload: dict[str, Any]):
        """Map a JSON import payload to (body, title, error)."""
        kind = str(payload.get("type") or "markdown").lower()
        if kind in ("markdown", "text"):
            body = str(payload.get("body") or "")
            title = str(payload.get("title") or "").strip()
            if not body:
                return None, None, "body is required for markdown/text import"
            return body, title, None
        if kind == "url":
            url = str(payload.get("url") or "").strip()
            if not url:
                return None, None, "url is required for url import"
            if not (url.startswith("http://") or url.startswith("https://")):
                return None, None, "url must start with http:// or https://"
            title = str(payload.get("title") or "").strip()
            try:
                import httpx
                import re as _re

                async with httpx.AsyncClient(
                    timeout=20.0, follow_redirects=True, headers={"User-Agent": "nanobot-wiki-importer/1.0"}
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    html = resp.text
                # Strip script/style blocks, then tags, then collapse whitespace.
                html = _re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<[^>]+>", " ", html)
                text = _re.sub(r"\s+", " ", text).strip()
                if not text:
                    return None, None, "fetched URL had no extractable text"
                if not title:
                    title_match = _re.search(r"<title[^>]*>(.*?)</title>", html, flags=_re.IGNORECASE | _re.DOTALL)
                    if title_match:
                        title = title_match.group(1).strip()
                return text[:32_000], title, None
            except Exception as exc:  # noqa: BLE001
                return None, None, f"failed to fetch URL: {exc}"
        return None, None, f"unsupported import type: {kind!r}"

    def _handle_wiki_edit(self, request: WsRequest) -> Response:
        """Edit an existing wiki page in place.

        Accepts ``{"slug": "...", "title": "...", "body": "...", "tags": [...]}``
        as JSON. Tags are merged with the page's existing tags so callers don't
        have to re-send the full tag set after each edit.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _http_error(400, f"invalid JSON body: {exc}")
        slug = str(payload.get("slug") or "").strip()
        if not slug:
            return _http_error(400, "slug is required")
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "")
        tags_raw = payload.get("tags")
        tags: list[str] | None = None
        if isinstance(tags_raw, list):
            tags = [str(t) for t in tags_raw if str(t).strip()]
        try:
            from nanobot.agent.wiki import WikiPaths, WikiStore
            workspace = Path(self.session_manager.workspace if self.session_manager else "~/.nanobot/workspace").expanduser().resolve()
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            existing = store.read_page(slug)
            if existing is None:
                return _http_error(404, f"page '{slug}' not found")
            # If the user supplied new tags, replace them outright (rather than
            # merge) so edits are predictable. ``write_page`` already merges
            # wikilinks detected in the new body, so we don't pass ``links``.
            page = store.write_page(
                slug=slug,
                title=title or existing.title,
                body=body,
                tags=tags if tags is not None else existing.fm.tags,
                source="manual-edit",
                merge_existing=False,
            )
            return _http_json_response({"ok": True, "page": page.to_dict()})
        except ValueError as exc:
            return _http_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, str(exc))

    def _handle_wiki_delete(self, request: WsRequest) -> Response:
        """Delete a wiki page by slug."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        slug = _query_first(query, "slug") or ""
        if not slug:
            return _http_error(400, "slug is required")
        try:
            from nanobot.agent.wiki import WikiPaths, WikiStore
            workspace = Path.home() / ".nanobot" / "workspace"
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            deleted = store.delete_page(slug)
            if deleted:
                return _http_json_response({"ok": True, "deleted": slug})
            else:
                return _http_error(404, f"Page '{slug}' not found")
        except Exception as e:
            return _http_error(500, str(e))

    async def _handle_wiki_lint(self, request: WsRequest) -> Response:
        """Run lint check on Wiki to find and fix issues."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            from nanobot.agent.wiki import WikiPaths, WikiStore
            from nanobot.agent.wiki.generator import WikiGenerator

            workspace = Path.home() / ".nanobot" / "workspace"
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            gen = WikiGenerator(store)

            # Run lint check
            result = await gen.lint_wiki(agent=None)
            return _http_json_response({"ok": True, "result": result})
        except Exception as e:
            return _http_error(500, str(e))

    async def _handle_wiki_regenerate(self, request: WsRequest) -> Response:
        """Trigger full Wiki regeneration using LLM.

        This runs the knowledge_sync cron job which:
        1. Syncs IMA content to Obsidian
        2. Uses WikiGenerator with LLM to create multiple Wiki pages per source
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        try:
            # Trigger the knowledge_sync cron job
            if self.cron_service:
                job = self.cron_service.get_job("knowledge_sync")
                if job:
                    # Run the job asynchronously
                    import asyncio
                    asyncio.create_task(self.cron_service.run_job("knowledge_sync", force=True))
                    return _http_json_response({
                        "ok": True,
                        "message": "Wiki regeneration triggered. This may take a few minutes.",
                        "job_id": "knowledge_sync"
                    })
                else:
                    return _http_error(404, "knowledge_sync job not found")
            else:
                return _http_error(503, "cron service unavailable")
        except Exception as e:
            return _http_error(500, str(e))

    def _handle_ima_status(self, request: WsRequest) -> Response:
        """Read IMA status directly from config.json for live updates."""
        import json as _json
        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            cfg = {}
        ima = cfg.get("tools", {}).get("ima", {})
        has_creds = bool(ima.get("clientId") and ima.get("apiKey"))
        return _http_json_response({
            "enabled": ima.get("enabled", False),
            "has_credentials": has_creds,
            "base_url": ima.get("baseUrl", "https://ima.qq.com"),
            "client_id": ima.get("clientId"),
            "api_key": ima.get("apiKey"),
        })

    async def _handle_ima_sync(self, request: WsRequest) -> Response:
        """Run IMA sync: fetch notes + KB items, LLM summarize, write to Obsidian Inbox."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        import traceback

        logs: list[str] = []
        try:
            from nanobot.config.loader import load_config
            from nanobot.providers.factory import make_provider
            from nanobot.agent.tools.ima._client import IMAClient
            from nanobot.agent.knowledge.pipeline import IMAIngestPipeline

            # Load full Config object
            try:
                config = load_config()
            except Exception as exc:
                return _http_json_response({"ok": False, "log": [f"ERROR: cannot load config: {exc}"]})

            # IMA credentials
            ima_cfg = getattr(config.tools, "ima", None)
            if not getattr(ima_cfg, "enabled", False):
                return _http_json_response({"ok": False, "log": ["ERROR: IMA is not enabled in config."]})
            if not ima_cfg.has_credentials():
                return _http_json_response({"ok": False, "log": ["ERROR: IMA credentials not configured."]})

            # Build IMAClient
            client = IMAClient.from_env_or_files(
                client_id=ima_cfg.client_id,
                api_key=ima_cfg.api_key,
                base_url=ima_cfg.base_url,
            )

            # Build LLM provider for summarization
            model = (self.runtime_model_name() or "").strip()
            if not model:
                model = config.resolve_preset().model.strip()
            if not model:
                return _http_json_response({"ok": False, "log": ["ERROR: No model configured for summarization."]})

            provider = make_provider(config, model=model)

            # Determine inbox path
            obs_cfg = getattr(config.tools, "obsidian", None)
            if getattr(obs_cfg, "enabled", False) and getattr(obs_cfg, "vault_path", None):
                vault = Path(obs_cfg.vault_path).expanduser().resolve()
            else:
                vault = Path(config.workspace_path).expanduser().resolve()
            vault_root = getattr(ima_cfg, "vault_root", "Nanobot") or "Nanobot"
            inbox_subdir = getattr(ima_cfg, "inbox_subdir", "Inbox") or "Inbox"
            inbox_root = vault / vault_root

            logs.append(f"IMA sync starting (model={model})")

            # Run the unified pipeline
            pipeline = IMAIngestPipeline(
                client=client,
                provider=provider,
                model=model,
                inbox_root=inbox_root,
                workspace=Path(config.workspace_path),
                inbox_dir_name=inbox_subdir,
            )

            result = await pipeline.run_all()

            for path in result.notes_written:
                logs.append(f"  OK: {path}")
            if result.errors:
                for err in result.errors:
                    logs.append(f"  WARN: {err}")

            logs.append(
                f"\nIMA sync complete: {result.items_processed} processed, "
                f"{result.items_skipped} skipped, {len(result.notes_written)} written"
            )
            return _http_json_response({
                "ok": True,
                "log": logs,
                "created": result.items_processed,
                "skipped": result.items_skipped,
            })

        except Exception as e:
            logs.append(f"FATAL: {traceback.format_exc()}")
            return _http_json_response({"ok": False, "log": logs})

    def _handle_obsidian_status(self, request: WsRequest) -> Response:
        """Read Obsidian status directly from config.json for live updates."""
        import json as _json
        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            cfg = {}
        obs = cfg.get("tools", {}).get("obsidian", {})
        vault_path = obs.get("vaultPath")
        file_count = 0
        if vault_path:
            vp = Path(vault_path).expanduser().resolve()
            if vp.exists():
                file_count = sum(1 for _ in vp.rglob("*.md"))
        return _http_json_response({
            "enabled": obs.get("enabled", False),
            "vault_path": vault_path,
            "last_sync_at": None,
            "file_count": file_count,
            "mode": obs.get("mode", "filesystem"),
        })

    async def _handle_obsidian_resync(self, request: WsRequest) -> Response:
        """Scan Obsidian vault and regenerate wiki pages using LLM.

        This triggers the full Wiki generation pipeline:
        1. Scan Obsidian vault for changed files
        2. For each changed file, use WikiGenerator to create multiple Wiki pages
        3. WikiGenerator uses LLM to extract concepts, create cross-references, etc.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        import json as _json

        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            return _http_error(500, "cannot read config")

        obs_cfg = cfg.get("tools", {}).get("obsidian", {})
        vault_path = obs_cfg.get("vaultPath")
        if not vault_path:
            return _http_json_response({"ok": False, "log": ["ERROR: Obsidian vault path not configured"]})

        vault = Path(vault_path).expanduser().resolve()
        if not vault.exists():
            return _http_json_response({"ok": False, "log": [f"ERROR: Vault path does not exist: {vault_path}"]})

        logs = []
        try:
            from nanobot.agent.wiki import WikiPaths, WikiStore
            from nanobot.agent.wiki.sync import ObsidianWikiSync

            workspace = Path.home() / ".nanobot" / "workspace"
            paths = WikiPaths.from_workspace(workspace)
            store = WikiStore(paths)
            vault_root = obs_cfg.get("nanobotRoot", "")

            sync = ObsidianWikiSync(
                store,
                vault_path=vault,
                vault_root=vault_root,
            )

            # Run sync - agent=None means just copy files, no LLM generation
            # The full LLM generation happens in the cron job (knowledge_sync)
            result = await sync.run_once(agent=None, max_files=50)

            logs.append(f"Scanning vault: {vault_path} ({result.scanned} files)")
            logs.append(f"Changed: {len(result.changed)} files")
            logs.append(f"Generated: {len(result.generated)} pages")
            logs.append(f"Skipped: {len(result.skipped)} files")
            if result.errors:
                for err in result.errors:
                    logs.append(f"  ERROR: {err}")

            # Note: Full LLM-powered Wiki generation happens in the cron job
            # Run: nanobot cron run knowledge_sync
            # Or wait for scheduled time: daily at 21:30 Beijing time

            return _http_json_response({
                "ok": True,
                "log": logs,
                "created": len(result.generated),
                "skipped": len(result.skipped),
            })

        except Exception as e:
            import traceback
            logs.append(f"FATAL: {traceback.format_exc()}")
            return _http_json_response({"ok": False, "log": logs})

    def _handle_weixin_qrcode(self, request: WsRequest) -> Response:
        """Start WeChat QR code login: fetch QR, return QR URL + qrcode_id for polling."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            import httpx as _httpx
            # Get WeChat base URL from config
            import json as _json
            config_path = Path.home() / ".nanobot" / "config.json"
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            base_url = cfg.get("channels", {}).get("weixin", {}).get("baseUrl", "https://ilinkai.weixin.qq.com")

            # Fetch QR code
            resp = _httpx.get(f"{base_url}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"}, timeout=15)
            data = resp.json()
            qrcode_id = data.get("qrcode", "")
            qrcode_img = data.get("qrcode_img_content", "")
            if not qrcode_id:
                return _http_json_response({"ok": False, "error": "Failed to get QR code"})
            return _http_json_response({"ok": True, "qrcode_id": qrcode_id, "qrcode_url": qrcode_img or qrcode_id})
        except Exception as e:
            return _http_json_response({"ok": False, "error": str(e)})

    def _handle_weixin_status(self, request: WsRequest) -> Response:
        """Poll WeChat QR code status. Returns status: wait/scaned/confirmed/expired."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        qrcode_id = _query_first(query, "qrcode_id") or ""

        # If no qrcode_id, return the current connection state
        if not qrcode_id:
            return _http_json_response(self._weixin_connection_state())

        try:
            import httpx as _httpx
            import json as _json
            config_path = Path.home() / ".nanobot" / "config.json"
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            base_url = cfg.get("channels", {}).get("weixin", {}).get("baseUrl", "https://ilinkai.weixin.qq.com")

            resp = _httpx.get(f"{base_url}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode_id}, timeout=15)
            data = resp.json()
            status = data.get("status", "wait")
            if status == "confirmed":
                token = data.get("bot_token", "")
                base = data.get("baseurl", "")
                if token:
                    cfg["channels"].setdefault("weixin", {}).update({
                        "enabled": True,
                        "token": token,
                        "baseUrl": base or base_url,
                    })
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(cfg, f, indent=2, ensure_ascii=False)
                return _http_json_response({"ok": True, "status": "confirmed", "has_token": bool(token)})
            return _http_json_response({"ok": True, "status": status})
        except Exception as e:
            return _http_json_response({"ok": False, "status": "error", "error": str(e)})

    def _weixin_connection_state(self) -> dict:
        """Check WeChat connection state: connected/connecting/disconnected."""
        import json as _json
        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            return {"connected": False, "has_token": False, "enabled": False}
        wx = cfg.get("channels", {}).get("weixin", {})
        has_token = bool(wx.get("token"))
        enabled = wx.get("enabled", False)
        return {"connected": enabled and has_token, "has_token": has_token, "enabled": enabled}

    # ----- WeChat Official Account Platform (微信公众平台) operator session -----

    def _handle_wxmp_status(self, request: WsRequest) -> Response:
        """Return current wxmp_article login state + credential age.

        Used by the Channels page to render a 96h-expiry warning.
        """
        from nanobot.channels.wxmp_platform import _LOGIN_TTL_S, load_credentials

        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        cred = load_credentials()
        now = time.time()
        if cred is None:
            # Distinguish "file missing" vs "credentials expired"
            state_file = Path.home() / ".nanobot" / "wxmp_article" / "wxmp_article_login.json"
            return _http_json_response({
                "logged_in": False,
                "expired": state_file.exists(),  # file exists but expired
                "login_at": None,
                "hours_since_login": None,
                "hours_until_expire": None,
                "ttl_hours": round(_LOGIN_TTL_S / 3600, 1),
                "token": "",
                "cookies_count": 0,
            })
        elapsed = now - cred.login_at
        remaining = max(0.0, _LOGIN_TTL_S - elapsed)
        expired = remaining <= 0
        return _http_json_response({
            "logged_in": not expired,
            "expired": expired,
            "login_at": int(cred.login_at),
            "hours_since_login": round(elapsed / 3600, 1),
            "hours_until_expire": round(remaining / 3600, 1),
            "ttl_hours": round(_LOGIN_TTL_S / 3600, 1),
            "token": cred.token,
            "cookies_count": len(cred.cookies),
        })

    async def _handle_wxmp_login(self, request: WsRequest) -> Response:
        """Launch the Playwright+Edge QR-scan login flow.

        Blocks until the user scans (typically 5-30s) or the 10-minute
        Playwright timeout fires. Uses ``asyncio.to_thread`` to run the
        synchronous Playwright sync API without blocking the gateway
        event loop. NOTE: this only works when the gateway runs on a
        machine with a display (where Edge can pop up); on a
        headless server, fall back to ``nanobot channels login
        wxmp_platform`` from an interactive shell.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        def _run_login() -> dict:
            from nanobot.channels.wxmp_platform import WxMpPlatformChannel
            channel = WxMpPlatformChannel({"workspace": str(Path.home() / ".nanobot")}, bus=None)
            ok = channel._login_sync(force=True)
            return {"ok": ok}

        try:
            result = await asyncio.to_thread(_run_login)
            return _http_json_response(result)
        except Exception as exc:
            return _http_json_response({"ok": False, "error": str(exc)})

    def _handle_wxmp_logout(self, request: WsRequest) -> Response:
        """Clear stored wxmp credentials (forces re-scan on next fetch)."""
        from nanobot.channels.wxmp_platform import clear_credentials

        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        deleted = clear_credentials()
        return _http_json_response({"ok": True, "deleted": deleted})

    def _handle_channels_list(self, request: WsRequest) -> Response:
        """List available channels with their enabled status."""
        import json as _json
        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            cfg = {}
        channels_cfg = cfg.get("channels", {})
        result = []
        for name in ["feishu", "weixin", "telegram", "discord", "slack", "whatsapp", "email"]:
            ch = channels_cfg.get(name, {})
            result.append({
                "name": name,
                "enabled": ch.get("enabled", False),
            })
        return _http_json_response({"channels": result})

    def _handle_channels_config(self, request: WsRequest) -> Response:
        """Return channel configurations for the web UI."""
        import json as _json
        config_path = Path.home() / ".nanobot" / "config.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        except Exception:
            cfg = {}
        channels_cfg = cfg.get("channels", {})
        result = {}
        for name in ["feishu", "weixin", "telegram", "discord", "slack", "whatsapp", "email"]:
            ch = channels_cfg.get(name, {})
            if ch:
                result[name] = {k: v for k, v in ch.items() if k != "token" and not k.endswith("Secret") and not k.endswith("Password")}
                result[name]["enabled"] = ch.get("enabled", False)
        return _http_json_response({"channels": result})

    def _handle_channels_save_config(self, request: WsRequest) -> Response:
        """Save IMA and Obsidian config to config.json via query params."""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")

        query = _parse_query(request.path)
        ima_client_id = _query_first(query, "ima_client_id") or ""
        ima_api_key = _query_first(query, "ima_api_key") or ""
        ima_enabled = (_query_first(query, "ima_enabled") or "").lower() == "true"
        obs_vault_path = _query_first(query, "obs_vault_path") or ""
        obs_enabled = (_query_first(query, "obs_enabled") or "").lower() == "true"
        feishu_enabled = (_query_first(query, "feishu_enabled") or "").lower() == "true"
        feishu_app_id = _query_first(query, "feishu_app_id") or ""
        feishu_app_secret = _query_first(query, "feishu_app_secret") or ""
        feishu_encrypt_key = _query_first(query, "feishu_encrypt_key") or ""
        feishu_verification_token = _query_first(query, "feishu_verification_token") or ""
        weixin_enabled = (_query_first(query, "weixin_enabled") or "").lower() == "true"
        weixin_token = _query_first(query, "weixin_token") or ""
        weixin_base_url = _query_first(query, "weixin_base_url") or ""

        config_path = Path.home() / ".nanobot" / "config.json"
        if not config_path.exists():
            return _http_error(500, "config.json not found")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            return _http_error(500, f"failed to read config: {e}")

        cfg.setdefault("tools", {}).setdefault("ima", {}).update({
            "enabled": ima_enabled,
            "clientId": ima_client_id or None,
            "apiKey": ima_api_key or None,
        })
        cfg.setdefault("tools", {}).setdefault("obsidian", {}).update({
            "enabled": obs_enabled,
            "vaultPath": obs_vault_path or None,
        })
        # Feishu
        channels = cfg.setdefault("channels", {})
        channels["feishu"] = channels.get("feishu", {})
        channels["feishu"].update({
            "enabled": feishu_enabled,
            "appId": feishu_app_id or "",
            "appSecret": feishu_app_secret or "",
            "encryptKey": feishu_encrypt_key or "",
            "verificationToken": feishu_verification_token or "",
        })
        # WeChat
        channels["weixin"] = channels.get("weixin", {})
        channels["weixin"].update({
            "enabled": weixin_enabled,
            "token": weixin_token or "",
            "baseUrl": weixin_base_url or "https://ilinkai.weixin.qq.com",
        })

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return _http_error(500, f"failed to write config: {e}")

        return _http_json_response({"ok": True, "message": "Config saved and applied immediately."})

    def _handle_plugins_list(self, request: WsRequest) -> Response:
        from nanobot.channels.registry import discover_all as discover_all_channels
        from nanobot.agent.tools.loader import ToolLoader

        channels = []
        try:
            all_channels = discover_all_channels()
            for name, cls in all_channels.items():
                channels.append({"name": name, "kind": "channel", "enabled": True})
        except Exception:
            pass

        tools = []
        try:
            loader = ToolLoader()
            registered = list(loader.discover().keys()) if hasattr(loader, "discover") else []
            for name in sorted(registered):
                tools.append({"name": name, "kind": "tool", "enabled": True})
        except Exception:
            pass

        return _http_json_response({"plugins": channels + tools})

    # -- Static file serving ------------------------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        assert self.static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self.static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self.static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            index = self.static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self._log.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        # Python's mimetypes on Windows returns text/plain for .js files,
        # which browsers reject for ES module scripts. Override explicitly.
        if candidate.name.endswith(".js"):
            ctype = "application/javascript"
        elif candidate.name.endswith(".css"):
            ctype = "text/css"
        elif ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )


def _automation_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    raw = _case_insensitive_header(request.headers, _AUTOMATION_VALUES_HEADER)
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return values if isinstance(values, dict) else None


def _parse_automation_update(
    values: dict[str, Any],
    *,
    current_job: CronJob | None = None,
) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    if "message" in values:
        raw_message = values.get("message")
        if not isinstance(raw_message, str):
            return "message must be a string"
        message = raw_message.strip()
        if not message:
            return "message cannot be empty"
        update["message"] = message
    if "schedule" in values:
        raw_schedule = values.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule must be an object"
        parsed_schedule = _parse_automation_schedule(raw_schedule)
        if isinstance(parsed_schedule, str):
            return parsed_schedule
        if current_job is not None and _schedule_matches_job(parsed_schedule, current_job):
            return update
        schedule_error = _validate_automation_schedule(parsed_schedule)
        if schedule_error:
            return schedule_error
        update["schedule"] = parsed_schedule
        update["delete_after_run"] = parsed_schedule.kind == "at"
    return update


def _parse_local_trigger_update(values: dict[str, Any]) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    forbidden = [key for key in ("message", "schedule") if key in values]
    if forbidden:
        return "local trigger updates only support name"
    return update


def _parse_automation_schedule(values: dict[str, Any]) -> CronSchedule | str:
    raw_kind = values.get("kind")
    if not isinstance(raw_kind, str):
        return "schedule kind must be a string"
    kind = raw_kind.strip()
    if kind == "every":
        every_ms = _positive_int(values.get("every_ms"))
        if every_ms is None:
            return "every schedule requires positive every_ms"
        return CronSchedule(kind="every", every_ms=every_ms)
    if kind == "cron":
        raw_expr = values.get("expr")
        if not isinstance(raw_expr, str):
            return "cron schedule requires expr"
        expr = raw_expr.strip()
        if not expr:
            return "cron schedule requires expr"
        raw_tz = values.get("tz")
        if raw_tz is not None and not isinstance(raw_tz, str):
            return "cron schedule timezone must be a string"
        tz = raw_tz.strip() if isinstance(raw_tz, str) else ""
        return CronSchedule(kind="cron", expr=expr, tz=tz or None)
    if kind == "at":
        at_ms = _positive_int(values.get("at_ms"))
        if at_ms is None:
            return "one-time schedule requires positive at_ms"
        return CronSchedule(kind="at", at_ms=at_ms)
    return "unknown schedule kind"


def _schedule_matches_job(schedule: CronSchedule, job: CronJob) -> bool:
    current = job.schedule
    if schedule.kind != current.kind:
        return False
    if schedule.kind == "at":
        return schedule.at_ms == current.at_ms
    if schedule.kind == "every":
        return schedule.every_ms == current.every_ms
    if schedule.kind == "cron":
        return (schedule.expr or "") == (current.expr or "") and (
            schedule.tz or None
        ) == (current.tz or None)
    return False


def _validate_automation_schedule(schedule: CronSchedule) -> str | None:
    if schedule.kind == "at":
        if not schedule.at_ms or schedule.at_ms <= int(time.time() * 1000):
            return "one-time schedule must be in the future"
        return None
    if schedule.kind != "cron":
        return None

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from croniter import croniter

        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
        base = datetime.now(tz=tz)
        croniter(schedule.expr, base).get_next(datetime)
    except Exception:
        return "cron schedule is invalid"
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _is_websocket_channel_session_key(key: str) -> bool:
    return key.startswith("websocket:")
