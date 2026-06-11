"""Mattermost gateway adapter.

Connects to a self-hosted (or cloud) Mattermost instance via its REST API
(v4) and WebSocket for real-time events.  No external Mattermost library
required — uses aiohttp which is already a Hermes dependency.

Environment variables:
    MATTERMOST_URL                      Server URL (e.g. https://mm.example.com)
    MATTERMOST_TOKEN                    Bot token or personal-access token
    MATTERMOST_ALLOWED_USERS            Comma-separated user IDs
    MATTERMOST_HOME_CHANNEL             Channel ID for cron/notification delivery
    MATTERMOST_CALLBACK_HOST            HTTP callback server bind address (default: 0.0.0.0)
    MATTERMOST_CALLBACK_PORT            HTTP callback server port (default: 8580)
    MATTERMOST_CALLBACK_URL             Public URL for Mattermost to POST action callbacks;
                                        auto-derived from host/port when not set
    MATTERMOST_SLASH_WEBHOOK_PUBLIC_URL Public URL for Mattermost to POST slash command
                                        invocations; auto-derived from MATTERMOST_CALLBACK_URL
                                        when not set
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# Mattermost post size limit (server default is 16383, but 4000 is the
# practical limit for readable messages — matching OpenClaw's choice).
MAX_POST_LENGTH = 4000

# Channel type codes returned by the Mattermost API.
_CHANNEL_TYPE_MAP = {
    "D": "dm",
    "G": "group",
    "P": "group",   # private channel → treat as group
    "O": "channel",
}

# Reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2

# Interactive-action callback server.
_CALLBACK_DEFAULT_PORT = 8580
_CALLBACK_DEFAULT_HOST = "0.0.0.0"
_CALLBACK_PATH = "/mattermost/actions"
# How long to wait for the Mattermost POST body before giving up.
_CALLBACK_READ_TIMEOUT_SECONDS = 10.0

# Slash-command webhook endpoint.
# Shares the callback server (port 8580 by default) — no separate server needed.
# The URL is auto-derived from MATTERMOST_CALLBACK_URL.
_SLASH_WEBHOOK_PATH = "/mattermost/slash"


def check_mattermost_requirements() -> bool:
    """Return True if the Mattermost adapter can be used."""
    token = os.getenv("MATTERMOST_TOKEN", "")
    url = os.getenv("MATTERMOST_URL", "")
    if not token:
        logger.debug("Mattermost: MATTERMOST_TOKEN not set")
        return False
    if not url:
        logger.warning("Mattermost: MATTERMOST_URL not set")
        return False
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        logger.warning("Mattermost: aiohttp not installed")
        return False


class MattermostAdapter(BasePlatformAdapter):
    """Gateway adapter for Mattermost (self-hosted or cloud)."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.MATTERMOST)

        self._base_url: str = (
            config.extra.get("url", "")
            or os.getenv("MATTERMOST_URL", "")
        ).rstrip("/")
        self._token: str = config.token or os.getenv("MATTERMOST_TOKEN", "")

        self._bot_user_id: str = ""
        self._bot_username: str = ""

        # aiohttp session + websocket handle
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None       # aiohttp.ClientWebSocketResponse
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False

        # Reply mode: "thread" to nest replies, "off" for flat messages.
        self._reply_mode: str = (
            config.extra.get("reply_mode", "")
            or os.getenv("MATTERMOST_REPLY_MODE", "off")
        ).lower()

        # Dedup cache (prevent reprocessing)
        self._dedup = MessageDeduplicator()

        # Interactive-action callback server.
        self._callback_host: str = os.getenv(
            "MATTERMOST_CALLBACK_HOST", _CALLBACK_DEFAULT_HOST
        )
        self._callback_port: int = int(
            os.getenv("MATTERMOST_CALLBACK_PORT", str(_CALLBACK_DEFAULT_PORT))
        )
        self._callback_url: str = os.getenv("MATTERMOST_CALLBACK_URL", "")
        self._callback_app: Any = None   # aiohttp.web.Application
        self._callback_runner: Any = None  # web.AppRunner
        self._callback_site: Any = None    # web.TCPSite

        # Per-prompt state keyed by a local monotonic counter value.
        self._approval_state: Dict[int, str] = {}     # counter → session_key
        self._slash_confirm_state: Dict[str, str] = {}  # confirm_id → session_key
        self._clarify_state: Dict[str, str] = {}       # clarify_id → ... (opaque)
        self._interactive_counter: int = 0

        # Slash-command registration (shares callback server, no separate webhook).
        self._slash_webhook_url: str = ""
        self._registered_slash_command_ids: List[str] = []

    def _thread_root_id(
        self,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Return the Mattermost root post ID for threaded sends."""
        if self._reply_mode != "thread":
            return None
        if metadata:
            thread_id = metadata.get("thread_id")
            if thread_id:
                return str(thread_id)
        if reply_to:
            return str(reply_to)
        return None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str) -> Dict[str, Any]:
        """GET /api/v4/{path}."""
        import aiohttp
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API GET %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("MM API GET %s network error: %s", path, exc)
            return {}

    async def _api_post(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /api/v4/{path} with JSON body."""
        import aiohttp
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.post(
                url, headers=self._headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API POST %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("MM API POST %s network error: %s", path, exc)
            return {}

    async def _api_put(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /api/v4/{path} with JSON body."""
        import aiohttp
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.put(
                url, headers=self._headers(), json=payload
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API PUT %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("MM API PUT %s network error: %s", path, exc)
            return {}

    async def _api_delete(self, path: str) -> bool:
        """DELETE /api/v4/{path}. Returns True on success (2xx)."""
        import aiohttp
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.delete(
                url, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API DELETE %s → %s: %s", path, resp.status, body[:200])
                    return False
                return True
        except aiohttp.ClientError as exc:
            logger.error("MM API DELETE %s network error: %s", path, exc)
            return False

    async def _upload_file(
        self, channel_id: str, file_data: bytes, filename: str, content_type: str = "application/octet-stream"
    ) -> Optional[str]:
        """Upload a file and return its file ID, or None on failure."""
        import aiohttp

        url = f"{self._base_url}/api/v4/files"
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field(
            "files",
            file_data,
            filename=filename,
            content_type=content_type,
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.post(url, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error("MM file upload → %s: %s", resp.status, body[:200])
                return None
            data = await resp.json()
            infos = data.get("file_infos", [])
            return infos[0]["id"] if infos else None

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Mattermost and start the WebSocket listener."""
        import aiohttp

        if not self._base_url or not self._token:
            logger.error("Mattermost: URL or token not configured")
            return False

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._closing = False

        # Verify credentials and fetch bot identity.
        me = await self._api_get("users/me")
        if not me or "id" not in me:
            logger.error("Mattermost: failed to authenticate — check MATTERMOST_TOKEN and MATTERMOST_URL")
            await self._session.close()
            return False

        self._bot_user_id = me["id"]
        self._bot_username = me.get("username", "")
        logger.info(
            "Mattermost: authenticated as @%s (%s) on %s",
            self._bot_username,
            self._bot_user_id,
            self._base_url,
        )

        # Start WebSocket in background.
        self._ws_task = asyncio.create_task(self._ws_loop())

        # Start the HTTP callback server for interactive actions
        # (also handles slash-command callbacks via /mattermost/slash).
        try:
            await self._start_callback_server()
        except Exception as exc:
            logger.warning(
                "Mattermost: callback server failed to start — "
                "interactive buttons will fall back to text: %s", exc,
            )

        # Register slash commands with Mattermost (webhook is on callback server).
        try:
            await self._register_slash_commands()
        except Exception as exc:
            logger.warning(
                "Mattermost: slash command registration failed: %s", exc,
            )

        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Mattermost."""
        self._closing = True

        # Stop the callback server (also handles slash-command webhook).
        await self._stop_callback_server()

        # Clean up slash commands.
        await self._cleanup_slash_commands()

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("Mattermost: disconnected")


    async def _resolve_root_id(self, post_id: str) -> str:
        """Resolve a post_id to the thread root_id for Mattermost.

        Mattermost requires root_id to be the *root* post of a thread.
        If the post is a reply (has its own root_id), we must use that
        root_id instead.  Using a reply's own ID as root_id causes
        "Invalid RootId parameter" errors.
        """
        if not post_id:
            return post_id
        # Check if this post has a root_id (meaning it's a reply)
        data = await self._api_get(f"posts/{post_id}")
        if data and data.get("root_id"):
            return data["root_id"]
        return post_id

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message (or multiple chunks) to a channel."""
        if not content:
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_POST_LENGTH)

        last_id = None
        for chunk in chunks:
            payload: Dict[str, Any] = {
                "channel_id": chat_id,
                "message": chunk,
            }
            # Thread support: metadata.thread_id is already the canonical thread root.
            # Otherwise resolve reply_to so Mattermost never receives a reply post as root_id.
            root_id = self._thread_root_id(None, metadata)
            if root_id:
                payload["root_id"] = root_id
            elif reply_to and self._reply_mode == "thread":
                payload["root_id"] = await self._resolve_root_id(reply_to)

            data = await self._api_post("posts", payload)
            if not data or "id" not in data:
                return SendResult(success=False, error="Failed to create post")
            last_id = data["id"]

        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return channel name and type."""
        data = await self._api_get(f"channels/{chat_id}")
        if not data:
            return {"name": chat_id, "type": "channel"}

        ch_type = _CHANNEL_TYPE_MAP.get(data.get("type", "O"), "channel")
        display_name = data.get("display_name") or data.get("name") or chat_id
        return {"name": display_name, "type": ch_type}

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a typing indicator.

        Includes ``parent_id`` when replying in a thread so the typing
        indicator appears inside the thread rather than the channel.
        """
        payload: Dict[str, Any] = {"channel_id": chat_id}
        if metadata and metadata.get("thread_id"):
            payload["parent_id"] = metadata["thread_id"]
        await self._api_post(
            f"users/{self._bot_user_id}/typing",
            payload,
        )

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit an existing post."""
        formatted = self.format_message(content)
        data = await self._api_put(
            f"posts/{message_id}/patch",
            {"message": formatted},
        )
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to edit post")
        return SendResult(success=True, message_id=data["id"])

    # ------------------------------------------------------------------
    # Interactive-action callback server
    # ------------------------------------------------------------------

    async def _start_callback_server(self) -> None:
        """Start the aiohttp HTTP server for interactive action callbacks
        and slash-command callbacks.

        This server receives POST requests from the Mattermost server when
        a user clicks an interactive message button, and when a user
        triggers a registered slash command.  The callback URL is
        either configured explicitly via ``MATTERMOST_CALLBACK_URL`` or
        derived from the bind host and port.  The slash webhook URL is
        auto-derived from the callback URL by substituting the path.
        """
        from aiohttp import web
        from urllib.parse import urlparse, urlunparse

        app = web.Application()

        # Health-check endpoint.
        app.router.add_get("/mattermost/health", self._health_check_handler)

        # Action callback endpoint.
        app.router.add_post(_CALLBACK_PATH, self._action_callback_handler)

        # Slash-command callback endpoint (shares the same server).
        app.router.add_post(_SLASH_WEBHOOK_PATH, self._handle_slash_webhook)

        self._callback_app = app
        self._callback_runner = web.AppRunner(app)
        await self._callback_runner.setup()
        self._callback_site = web.TCPSite(
            self._callback_runner, self._callback_host, self._callback_port,
        )
        await self._callback_site.start()

        # Derive the public callback URL if not explicitly set.
        if not self._callback_url:
            self._callback_url = (
                f"http://{self._callback_host}:{self._callback_port}"
                f"{_CALLBACK_PATH}"
            )
            # If the host is 0.0.0.0, hint that the user should set the URL explicitly.
            if self._callback_host in {"0.0.0.0", "::"}:
                logger.info(
                    "Mattermost: callback server listening on %s:%s — "
                    "set MATTERMOST_CALLBACK_URL if Mattermost cannot reach "
                    "this address directly",
                    self._callback_host, self._callback_port,
                )
        logger.info(
            "Mattermost: callback server ready at %s", self._callback_url,
        )

        # Auto-derive the slash webhook URL from the callback URL.
        parsed = urlparse(self._callback_url)
        self._slash_webhook_url = urlunparse(parsed._replace(path=_SLASH_WEBHOOK_PATH))
        logger.info(
            "Mattermost: slash webhook URL derived as %s",
            self._slash_webhook_url,
        )

    async def _stop_callback_server(self) -> None:
        """Stop the interactive-action callback server."""
        if self._callback_site:
            try:
                await self._callback_site.stop()
            except Exception as exc:
                logger.debug("Mattermost: callback site stop: %s", exc)
            self._callback_site = None
        if self._callback_runner:
            try:
                await self._callback_runner.cleanup()
            except Exception as exc:
                logger.debug("Mattermost: callback runner cleanup: %s", exc)
            self._callback_runner = None
        self._callback_app = None
        logger.info("Mattermost: callback server stopped")

    async def _health_check_handler(self, request: Any) -> Any:
        """Simple health check for the callback server."""
        from aiohttp import web
        return web.json_response({"status": "ok", "adapter": "mattermost"})

    async def _action_callback_handler(self, request: Any) -> Any:
        """Handle an incoming Mattermost interactive-action POST.

        Mattermost sends this when a user clicks an interactive message
        button.  The body contains the action context we embedded when
        creating the message.
        """
        from aiohttp import web

        try:
            data = await request.json()
        except Exception:
            logger.warning("Mattermost: invalid action callback body")
            return web.json_response(
                {"error": "invalid body"}, status=400,
            )

        context = data.get("context", {}) or {}
        action_type = context.get("action_type", "")
        user_name = data.get("user_name", data.get("user_id", "Unknown"))

        logger.info(
            "Mattermost: action callback type=%s user=%s",
            action_type, user_name,
        )

        if action_type == "exec_approval":
            return await self._handle_exec_approval_callback(data, context, user_name)
        elif action_type == "slash_confirm":
            return await self._handle_slash_confirm_callback(data, context, user_name)
        elif action_type == "clarify":
            return await self._handle_clarify_callback(data, context, user_name)
        elif action_type == "update_prompt":
            return await self._handle_update_prompt_callback(data, context, user_name)
        else:
            logger.warning(
                "Mattermost: unknown action_type %r", action_type,
            )
            return web.json_response({"error": "unknown action"}, status=400)

    async def _build_update_response(
        self, user_name: str, label: str,
    ) -> Any:
        """Build an aiohttp JSON response that updates the original post.

        Mattermost expects a JSON body from the action callback; the
        ``update`` key replaces the original post's content.
        """
        from aiohttp import web
        return web.json_response({
            "update": {
                "message": f"{label} by @{user_name}",
                "props": {},
                "attachments": [],
            },
        })

    async def _handle_exec_approval_callback(
        self, data: dict, context: dict, user_name: str,
    ) -> Any:
        """Resolve a pending exec-approval from a button click."""
        from aiohttp import web

        approval_id = context.get("approval_id")
        choice = context.get("choice", "deny")
        if approval_id is None:
            return web.json_response({"error": "missing approval_id"}, status=400)

        session_key = self._approval_state.pop(approval_id, None)
        if not session_key:
            return web.json_response({
                "update": {
                    "message": "This approval has already been resolved.",
                    "props": {},
                    "attachments": [],
                },
            })

        label_map = {
            "once": "✅ Approved once",
            "session": "✅ Approved for session",
            "always": "✅ Approved permanently",
            "deny": "❌ Denied",
        }
        label = label_map.get(choice, "Resolved")

        from tools.approval import resolve_gateway_approval
        try:
            count = resolve_gateway_approval(session_key, choice)
            logger.info(
                "Mattermost button resolved %d approval(s) for session %s "
                "(choice=%s, user=%s)",
                count, session_key, choice, user_name,
            )
        except Exception as exc:
            logger.error(
                "Mattermost: failed to resolve approval: %s", exc,
            )

        return await self._build_update_response(user_name, label)

    async def _handle_slash_confirm_callback(
        self, data: dict, context: dict, user_name: str,
    ) -> Any:
        """Resolve a pending slash-confirm from a button click."""
        from aiohttp import web

        confirm_id = context.get("confirm_id")
        choice = context.get("choice", "cancel")
        if not confirm_id:
            return web.json_response({"error": "missing confirm_id"}, status=400)

        session_key = self._slash_confirm_state.pop(confirm_id, None)
        if not session_key:
            return web.json_response({
                "update": {
                    "message": "This prompt has already been resolved.",
                    "props": {},
                    "attachments": [],
                },
            })

        label_map = {
            "once": "✅ Approved once",
            "always": "🔒 Always approve",
            "cancel": "❌ Cancelled",
        }
        label = label_map.get(choice, "Resolved")

        from tools import slash_confirm as _sc
        try:
            result_text = await _sc.resolve(session_key, confirm_id, choice)
            if result_text:
                logger.info(
                    "Mattermost slash-confirm result: %s", result_text,
                )
        except Exception as exc:
            logger.error(
                "Mattermost: failed to resolve slash confirm: %s", exc,
            )

        return await self._build_update_response(user_name, label)

    async def _handle_clarify_callback(
        self, data: dict, context: dict, user_name: str,
    ) -> Any:
        """Resolve a pending clarify from a button click."""
        from aiohttp import web

        clarify_id = context.get("clarify_id")
        response = context.get("response", "")
        if not clarify_id:
            return web.json_response({"error": "missing clarify_id"}, status=400)

        from tools.clarify_gateway import (
            resolve_gateway_clarify,
            mark_awaiting_text,
        )

        if response == "__other__":
            mark_awaiting_text(clarify_id)
            label = "✏️ Awaiting your typed answer"
        else:
            resolve_gateway_clarify(clarify_id, response)
            label = f"✅ You chose: {response}"

        return await self._build_update_response(user_name, label)

    async def _handle_update_prompt_callback(
        self, data: dict, context: dict, user_name: str,
    ) -> Any:
        """Handle an update-prompt button click (Yes / No)."""
        from aiohttp import web

        choice = context.get("choice", "n")
        label = "✅ Yes" if choice == "y" else "❌ No"
        return await self._build_update_response(user_name, label)

    # ------------------------------------------------------------------
    # Interactive messages (buttons)
    # ------------------------------------------------------------------

    async def _send_interactive_post(
        self,
        chat_id: str,
        message: str,
        attachments: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a post with interactive message attachments (action buttons).

        This is the common send helper for all button-based prompts.
        """
        payload: Dict[str, Any] = {
            "channel_id": chat_id,
            "message": message,
            "props": {"attachments": attachments},
        }
        root_id = self._thread_root_id(None, metadata)
        if root_id:
            payload["root_id"] = root_id
        data = await self._api_post("posts", payload)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to create interactive post")
        return SendResult(success=True, message_id=data["id"])

    def _next_interactive_id(self) -> int:
        """Return a monotonic counter for interactive prompts."""
        self._interactive_counter += 1
        return self._interactive_counter

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a button-based exec-approval prompt for a dangerous command.

        Four buttons: Allow Once, Session, Always, Deny.
        Button clicks call ``resolve_gateway_approval()`` via the callback
        server to unblock the waiting agent thread.
        """
        if not self._callback_runner:
            logger.warning(
                "Mattermost: callback server not running — "
                "falling back to text approval",
            )
            return SendResult(success=False, error="Callback server not running")

        cmd_preview = command[:3800] + "..." if len(command) > 3800 else command
        attachment_text = f"```\\n{cmd_preview}\\n```\\n\\nReason: {description}"

        approval_id = self._next_interactive_id()
        self._approval_state[approval_id] = session_key

        button = lambda name, choice: {
            "name": name,
            "integration": {
                "url": self._callback_url,
                "context": {
                    "action_type": "exec_approval",
                    "approval_id": approval_id,
                    "choice": choice,
                },
            },
            "type": "button",
        }

        attachments = [{
            "title": "⚠️ Command Approval Required",
            "text": attachment_text,
            "actions": [
                button("✅ Allow Once", "once"),
                button("✅ Session", "session"),
                button("✅ Always", "always"),
                button("❌ Deny", "deny"),
            ],
        }]

        return await self._send_interactive_post(
            chat_id, "", attachments, metadata,
        )

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a three-button slash-command confirmation prompt.

        Buttons: Approve Once, Always Approve, Cancel.
        """
        if not self._callback_runner:
            return SendResult(success=False, error="Callback server not running")

        body = message[:3800] + "..." if len(message) > 3800 else message
        self._slash_confirm_state[confirm_id] = session_key

        button = lambda name, choice: {
            "name": name,
            "integration": {
                "url": self._callback_url,
                "context": {
                    "action_type": "slash_confirm",
                    "confirm_id": confirm_id,
                    "choice": choice,
                },
            },
            "type": "button",
        }

        attachments = [{
            "title": title or "Confirm",
            "text": body,
            "actions": [
                button("✅ Approve Once", "once"),
                button("🔒 Always Approve", "always"),
                button("❌ Cancel", "cancel"),
            ],
        }]

        return await self._send_interactive_post(
            chat_id, "", attachments, metadata,
        )

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[List[str]],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a clarify prompt with choice buttons.

        In multi-choice mode, renders one button per option plus a final
        "✏️ Other" button for free-text input.  In open-ended mode, falls
        back to plain text.
        """
        if not self._callback_runner:
            return SendResult(success=False, error="Callback server not running")

        clean_choices = [
            str(c).strip() for c in (choices or [])
            if c is not None and str(c).strip()
        ]
        # Mattermost allows up to 5 actions per attachment row,
        # with up to 20 total. Cap at 19 (reserve one for "Other").
        clean_choices = clean_choices[:19]

        if clean_choices:
            button = lambda name, resp: {
                "name": name,
                "integration": {
                    "url": self._callback_url,
                    "context": {
                        "action_type": "clarify",
                        "clarify_id": clarify_id,
                        "response": resp,
                    },
                },
                "type": "button",
            }

            actions = [button(c, c) for c in clean_choices]
            actions.append(button("✏️ Other (type answer)", "__other__"))

            attachments = [{
                "title": "❓ Hermes needs your input",
                "text": question,
                "actions": actions,
            }]

            return await self._send_interactive_post(
                chat_id, "", attachments, metadata,
            )

        # Open-ended: no choices, fall back to base text handling.
        return await super().send_clarify(
            chat_id, question, choices, clarify_id, session_key, metadata,
        )

    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Yes / No update prompt (used by the /update watcher)."""
        if not self._callback_runner:
            return SendResult(success=False, error="Callback server not running")

        default_hint = f" (default: {default})" if default else ""
        text = f"⚕ *Update needs your input:*\\n\\n{prompt}{default_hint}"

        button = lambda name, choice: {
            "name": name,
            "integration": {
                "url": self._callback_url,
                "context": {
                    "action_type": "update_prompt",
                    "choice": choice,
                },
            },
            "type": "button",
        }

        attachments = [{
            "title": "Update",
            "text": text,
            "actions": [
                button("✓ Yes", "y"),
                button("✗ No", "n"),
            ],
        }]

        return await self._send_interactive_post(
            chat_id, "", attachments, metadata,
        )

    # ------------------------------------------------------------------
    # Slash-command handler (endpoint is on the callback server)
    # ------------------------------------------------------------------

    async def _handle_slash_webhook(self, request: Any) -> Any:
        """Handle an incoming slash-command POST from Mattermost.

        The body contains:

        - ``channel_id`` — channel the command was typed in
        - ``user_id`` — who triggered it
        - ``user_name`` — display name
        - ``command`` — e.g. ``/help``
        - ``text`` — everything after the command trigger
        - ``team_id`` — Mattermost team

        We construct a ``MessageEvent`` with the full command text and
        route it through the normal message-handling pipeline so the
        agent processes it just like a WebSocket-posted message.
        """
        from aiohttp import web

        try:
            data = await request.json()
        except Exception:
            logger.warning("Mattermost: invalid slash webhook body")
            return web.json_response(
                {"error": "invalid body"}, status=400,
            )

        channel_id = data.get("channel_id", "")
        user_id = data.get("user_id", "")
        user_name = data.get("user_name", user_id)
        command = data.get("command", "").strip()
        text = data.get("text", "").strip()

        if not command or not channel_id:
            logger.warning("Mattermost: slash webhook missing command or channel_id")
            return web.json_response(
                {"error": "missing fields"}, status=400,
            )

        # Reconstruct the full message text as the user typed it.
        full_text = command
        if text:
            full_text = f"{command} {text}"

        logger.info(
            "Mattermost: slash command %r from @%s in %s",
            full_text, user_name, channel_id,
        )

        # Build source info.
        source = self.build_source(
            chat_id=channel_id,
            chat_type="channel",
            user_id=user_id,
            user_name=user_name,
            thread_id=None,
        )

        # Resolve per-channel prompt.
        from gateway.platforms.base import resolve_channel_prompt
        _channel_prompt = resolve_channel_prompt(
            self.config.extra, channel_id, None,
        )

        msg_event = MessageEvent(
            text=full_text,
            message_type=MessageType.COMMAND,
            source=source,
            raw_message=data,
            message_id="",
            media_urls=None,
            media_types=None,
            channel_prompt=_channel_prompt,
        )

        # Fire-and-forget: the agent processes the command asynchronously.
        # We respond quickly so Mattermost doesn't time out the webhook.
        asyncio.ensure_future(self.handle_message(msg_event))

        return web.json_response({
            "response_type": "ephemeral",
            "text": "⏳ Processing...",
        })

    async def _register_slash_commands(self) -> None:
        """Register all gateway-available commands with Mattermost.

        Iterates ``COMMAND_REGISTRY`` from the CLI and registers each
        command that is available on the gateway via ``POST /api/v4/commands``.
        Registered command IDs are stored so they can be cleaned up on
        disconnect.

        The webhook URL is set to ``self._slash_webhook_url``, which is
        auto-derived from the callback server URL during startup.
        """
        if not self._slash_webhook_url:
            logger.info(
                "Mattermost: slash webhook URL not available — "
                "slash command autocomplete disabled",
            )
            return

        # Collect gateway-available commands from COMMAND_REGISTRY.
        try:
            from hermes_cli.commands import (
                COMMAND_REGISTRY,
                _is_gateway_available,
                _resolve_config_gates,
            )
        except ImportError:
            logger.warning(
                "Mattermost: could not import COMMAND_REGISTRY — "
                "slash commands not registered",
            )
            return

        # Fetch the user's teams to register commands per-team.
        teams_data = await self._api_get(f"users/{self._bot_user_id}/teams")
        if not teams_data or not isinstance(teams_data, list):
            logger.warning(
                "Mattermost: no teams found — slash commands not registered",
            )
            return

        config_overrides = _resolve_config_gates()
        webhook_url = self._slash_webhook_url
        registered_count = 0

        for team_info in teams_data:
            team_id = team_info.get("id")
            if not team_id:
                continue

            for cmd in COMMAND_REGISTRY:
                if not _is_gateway_available(cmd, config_overrides):
                    continue
                # Mattermost custom slash commands use the trigger word;
                # use the canonical command name.
                trigger = cmd.name.lower().replace("_", "-")
                description = cmd.description[:64]  # Mattermost limit

                payload = {
                    "team_id": team_id,
                    "trigger": trigger,
                    "url": webhook_url,
                    "method": "P",
                    "display_name": f"/{cmd.name}",
                    "description": description,
                    "auto_complete": True,
                    "auto_complete_desc": description,
                    "auto_complete_hint": cmd.args_hint or "",
                }

                result = await self._api_post("commands", payload)
                if result and "id" in result:
                    self._registered_slash_command_ids.append(result["id"])
                    registered_count += 1
                else:
                    logger.debug(
                        "Mattermost: failed to register slash command /%s "
                        "(may already exist)", cmd.name,
                    )

        if registered_count:
            logger.info(
                "Mattermost: registered %d slash command(s) across %d team(s)",
                registered_count, len(teams_data),
            )

    async def _cleanup_slash_commands(self) -> None:
        """Delete previously registered slash commands on disconnect."""
        if not self._registered_slash_command_ids:
            return

        deleted = 0
        for cmd_id in self._registered_slash_command_ids:
            if await self._api_delete(f"commands/{cmd_id}"):
                deleted += 1

        if deleted:
            logger.info(
                "Mattermost: cleaned up %d/%d registered slash command(s)",
                deleted, len(self._registered_slash_command_ids),
            )
        self._registered_slash_command_ids.clear()

    async def _add_reaction(
        self, post_id: str, emoji_name: str,
    ) -> bool:
        """Add a reaction emoji to a post.

        Calls ``POST /api/v4/posts/{post_id}/reactions``.
        Returns True if the reaction was added successfully.
        """
        if not post_id or not self._bot_user_id:
            return False
        payload = {
            "user_id": self._bot_user_id,
            "post_id": post_id,
            "emoji_name": emoji_name.lstrip(":").rstrip(":"),
        }
        result = await self._api_post(f"posts/{post_id}/reactions", payload)
        return bool(result)

    async def _on_message_queued(self, event: MessageEvent) -> None:
        """Acknowledge queued messages with an hourglass reaction."""
        if not event.message_id:
            return
        try:
            await self._add_reaction(event.message_id, "hourglass_flowing_sand")
        except Exception as exc:
            logger.debug("Mattermost: failed to add queue reaction: %s", exc)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image and upload it as a file attachment."""
        return await self._send_url_as_file(
            chat_id, image_url, caption, reply_to, "image", metadata
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image file."""
        return await self._send_local_file(
            chat_id, image_path, caption, reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file as a document."""
        return await self._send_local_file(
            chat_id, file_path, caption, reply_to, file_name, metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload an audio file."""
        return await self._send_local_file(
            chat_id, audio_path, caption, reply_to, metadata=metadata
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a video file."""
        return await self._send_local_file(
            chat_id, video_path, caption, reply_to, metadata=metadata
        )

    def format_message(self, content: str) -> str:
        """Mattermost uses standard Markdown — mostly pass through.

        Strip image markdown into plain links (files are uploaded separately).
        """
        # Convert ![alt](url) to just the URL — Mattermost renders
        # image URLs as inline previews automatically.
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        return content

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _send_url_as_file(
        self,
        chat_id: str,
        url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        kind: str = "file",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download a URL and upload it as a file attachment."""
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            logger.warning("Mattermost: blocked unsafe URL (SSRF protection)")
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata)

        import aiohttp

        file_data = None
        ct = "application/octet-stream"
        fname = url.rsplit("/", 1)[-1].split("?")[0] or f"{kind}.png"

        for attempt in range(3):
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 500 or resp.status == 429:
                        if attempt < 2:
                            logger.debug("Mattermost download retry %d/2 for %s (status %d)",
                                         attempt + 1, url[:80], resp.status)
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                    if resp.status >= 400:
                        return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata)
                    file_data = await resp.read()
                    ct = resp.content_type or "application/octet-stream"
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("Mattermost: failed to download %s after %d attempts: %s", url, attempt + 1, exc)
                return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata)

        if file_data is None:
            logger.warning("Mattermost: download returned no data for %s", url)
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata)

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata)

        payload: Dict[str, Any] = {
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        }
        root_id = self._thread_root_id(None, metadata)
        if root_id:
            payload["root_id"] = root_id
        elif reply_to and self._reply_mode == "thread":
            payload["root_id"] = await self._resolve_root_id(reply_to)

        data = await self._api_post("posts", payload)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        file_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file and attach it to a post."""
        import mimetypes

        p = Path(file_path)
        if not p.exists():
            return await self.send(
                chat_id,
                f"{caption or ''}\n(file not found: {file_path})",
                reply_to,
                metadata,
            )

        fname = file_name or p.name
        ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        file_data = p.read_bytes()

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return SendResult(success=False, error="File upload failed")

        payload: Dict[str, Any] = {
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        }
        root_id = self._thread_root_id(None, metadata)
        if root_id:
            payload["root_id"] = root_id
        elif reply_to and self._reply_mode == "thread":
            payload["root_id"] = await self._resolve_root_id(reply_to)

        data = await self._api_post("posts", payload)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single Mattermost post with multiple attachments.

        Mattermost supports up to 5 ``file_ids`` per post. Each image is
        uploaded individually (Mattermost's file API is one-at-a-time),
        then a single post is created referencing all uploaded file_ids
        at once. Batches larger than 5 are chunked. Falls back to the
        base per-image loop on total failure.
        """
        if not images:
            return

        import mimetypes
        import aiohttp
        from urllib.parse import unquote as _unquote

        CHUNK = 5  # Mattermost post file_ids cap
        chunks = [images[i:i + CHUNK] for i in range(0, len(images), CHUNK)]

        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)

            file_ids: List[str] = []
            caption_parts: List[str] = []
            try:
                for image_url, alt_text in chunk:
                    if alt_text:
                        caption_parts.append(alt_text)

                    if image_url.startswith("file://"):
                        local_path = _unquote(image_url[7:])
                        p = Path(local_path)
                        if not p.exists():
                            logger.warning("Mattermost: skipping missing image %s", local_path)
                            continue
                        fname = p.name
                        ct = mimetypes.guess_type(fname)[0] or "image/png"
                        file_data = p.read_bytes()
                    else:
                        from tools.url_safety import is_safe_url
                        if not is_safe_url(image_url):
                            logger.warning("Mattermost: blocked unsafe image URL in batch")
                            continue
                        try:
                            async with self._session.get(
                                image_url, timeout=aiohttp.ClientTimeout(total=30)
                            ) as resp:
                                if resp.status >= 400:
                                    logger.warning(
                                        "Mattermost: failed to download image (HTTP %d): %s",
                                        resp.status, image_url[:80],
                                    )
                                    continue
                                file_data = await resp.read()
                                ct = resp.content_type or "image/png"
                        except Exception as dl_err:
                            logger.warning("Mattermost: download failed for %s: %s", image_url[:80], dl_err)
                            continue
                        fname = image_url.rsplit("/", 1)[-1].split("?")[0] or f"image_{len(file_ids)}.png"

                    fid = await self._upload_file(chat_id, file_data, fname, ct)
                    if fid:
                        file_ids.append(fid)

                if not file_ids:
                    continue

                payload: Dict[str, Any] = {
                    "channel_id": chat_id,
                    "message": "\n".join(caption_parts),
                    "file_ids": file_ids,
                }
                root_id = self._thread_root_id(None, metadata)
                if root_id:
                    payload["root_id"] = root_id
                logger.info(
                    "Mattermost: sending %d image(s) as single post (chunk %d/%d)",
                    len(file_ids), chunk_idx + 1, len(chunks),
                )
                data = await self._api_post("posts", payload)
                if not data or "id" not in data:
                    logger.warning("Mattermost: multi-image post failed, falling back")
                    await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)
            except Exception as e:
                logger.warning(
                    "Mattermost: multi-image send failed (chunk %d/%d), falling back: %s",
                    chunk_idx + 1, len(chunks), e, exc_info=True,
                )
                await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Connect to the WebSocket and listen for events, reconnecting on failure."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self._ws_connect_and_listen()
                # Clean disconnect — reset delay.
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # Detect permanent auth/permission failures that will never
                # succeed on retry — stop reconnecting instead of looping forever.
                import aiohttp
                err_str = str(exc).lower()
                if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in {401, 403}:
                    logger.error("Mattermost WS auth failed (HTTP %d) — stopping reconnect", exc.status)
                    return
                if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                    logger.error("Mattermost WS permanent error: %s — stopping reconnect", exc)
                    return
                logger.warning("Mattermost WS error: %s — reconnecting in %.0fs", exc, delay)

            if self._closing:
                return

            # Exponential backoff with jitter.
            import random
            jitter = delay * _RECONNECT_JITTER * random.random()
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _ws_connect_and_listen(self) -> None:
        """Single WebSocket session: connect, authenticate, process events."""
        # Build WS URL: https:// → wss://, http:// → ws://
        ws_url = re.sub(r"^http", "ws", self._base_url) + "/api/v4/websocket"
        logger.info("Mattermost: connecting to %s", ws_url)

        self._ws = await self._session.ws_connect(ws_url, heartbeat=30.0)

        # Authenticate via the WebSocket.
        auth_msg = {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {"token": self._token},
        }
        await self._ws.send_json(auth_msg)
        logger.info("Mattermost: WebSocket connected and authenticated")

        async for raw_msg in self._ws:
            if self._closing:
                return

            if raw_msg.type in {
                raw_msg.type.TEXT,
                raw_msg.type.BINARY,
            }:
                try:
                    event = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ws_event(event)
            elif raw_msg.type in {
                raw_msg.type.ERROR,
                raw_msg.type.CLOSE,
                raw_msg.type.CLOSING,
                raw_msg.type.CLOSED,
            }:
                logger.info("Mattermost: WebSocket closed (%s)", raw_msg.type)
                break

    async def _handle_ws_event(self, event: Dict[str, Any]) -> None:
        """Process a single WebSocket event."""
        event_type = event.get("event")
        if event_type != "posted":
            return

        data = event.get("data", {})
        raw_post_str = data.get("post")
        if not raw_post_str:
            return

        try:
            post = json.loads(raw_post_str)
        except (json.JSONDecodeError, TypeError):
            return

        # Ignore own messages.
        if post.get("user_id") == self._bot_user_id:
            return

        # Ignore system posts.
        if post.get("type"):
            return

        post_id = post.get("id", "")

        # Dedup.
        if self._dedup.is_duplicate(post_id):
            return

        # Build message event.
        channel_id = post.get("channel_id", "")
        channel_type_raw = data.get("channel_type", "O")
        chat_type = _CHANNEL_TYPE_MAP.get(channel_type_raw, "channel")

        # For DMs, user_id is sufficient.  For channels, check for @mention.
        message_text = post.get("message", "")

        # Mattermost WebSocket post payloads can omit root_id for thread
        # replies. Resolve it before mention gating so follow-up replies in an
        # existing Hermes thread are not dropped as unmentioned channel posts.
        resolved_root_id = post.get("root_id") or None
        if not resolved_root_id and channel_type_raw != "D" and post_id:
            try:
                full_post = await self._api_get(f"posts/{post_id}")
                resolved_root_id = (full_post.get("root_id") or None) if full_post else None
            except Exception as exc:
                logger.debug("Mattermost: could not resolve root_id for post %s: %s", post_id, exc)

        # Mention-gating for non-DM channels.
        # Config (config.yaml `mattermost.*` with env-var fallback):
        #   require_mention / MATTERMOST_REQUIRE_MENTION: Require @mention in channels (default: true)
        #   free_response_channels / MATTERMOST_FREE_RESPONSE_CHANNELS: Channel IDs where bot responds without mention
        #   allowed_channels / MATTERMOST_ALLOWED_CHANNELS: If set, bot ONLY responds in these channels (whitelist)
        if channel_type_raw != "D":
            # allowed_channels check (whitelist - must pass before other gating).
            # When set, messages from channels NOT in this list are silently
            # ignored, even if @mentioned. DMs are already excluded above.
            allowed_raw = self.config.extra.get("allowed_channels") if self.config.extra else None
            if allowed_raw is None:
                allowed_raw = os.getenv("MATTERMOST_ALLOWED_CHANNELS", "")
            if isinstance(allowed_raw, list):
                allowed_channels = {str(c).strip() for c in allowed_raw if str(c).strip()}
            else:
                allowed_channels = {
                    c.strip() for c in str(allowed_raw).split(",") if c.strip()
                }
            if allowed_channels and channel_id not in allowed_channels:
                logger.debug(
                    "Mattermost: ignoring message in non-allowed channel: %s",
                    channel_id,
                )
                return

            require_mention = os.getenv(
                "MATTERMOST_REQUIRE_MENTION", "true"
            ).lower() not in {"false", "0", "no"}

            free_channels_raw = os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "")
            free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
            is_free_channel = channel_id in free_channels

            mention_patterns = [
                f"@{self._bot_username}",
                f"@{self._bot_user_id}",
            ]
            has_mention = any(
                pattern.lower() in message_text.lower()
                for pattern in mention_patterns
            )

            thread_allows_followup = False
            thread_root_id = resolved_root_id or ""
            if require_mention and not is_free_channel and not has_mention and thread_root_id:
                root_post = await self._api_get(f"posts/{thread_root_id}")
                root_message = root_post.get("message", "") if root_post else ""
                root_user_id = root_post.get("user_id", "") if root_post else ""
                thread_allows_followup = (
                    root_user_id == self._bot_user_id
                    or any(
                        pattern.lower() in root_message.lower()
                        for pattern in mention_patterns
                    )
                )

            if (
                require_mention
                and not is_free_channel
                and not has_mention
                and not thread_allows_followup
            ):
                logger.debug(
                    "Mattermost: skipping non-DM message without @mention (channel=%s)",
                    channel_id,
                )
                return

            # Strip @mention from the message text so the agent sees clean input.
            if has_mention:
                for pattern in mention_patterns:
                    message_text = re.sub(
                        re.escape(pattern), "", message_text, flags=re.IGNORECASE
                    ).strip()

        # Resolve sender info.
        sender_id = post.get("user_id", "")
        sender_name = data.get("sender_name", "").lstrip("@") or sender_id

        # Thread support:
        # Mattermost threads are not separate channels. A thread is a normal
        # channel post with root_id set to the root post. Normalize accepted
        # top-level channel messages to their own post id when threaded replies
        # are enabled so every downstream send path sees a stable thread_id.
        thread_id = resolved_root_id
        if (
            not thread_id
            and channel_type_raw != "D"
            and self._reply_mode == "thread"
            and post_id
        ):
            thread_id = post_id

        # Determine message type.
        file_ids = post.get("file_ids") or []
        msg_type = MessageType.TEXT
        if message_text.startswith("/"):
            msg_type = MessageType.COMMAND

        # Download file attachments immediately (URLs require auth headers
        # that downstream tools won't have).
        media_urls: List[str] = []
        media_types: List[str] = []
        for fid in file_ids:
            try:
                file_info = await self._api_get(f"files/{fid}/info")
                fname = file_info.get("name", f"file_{fid}")
                ext = Path(fname).suffix or ""
                mime = file_info.get("mime_type", "application/octet-stream")

                import aiohttp
                dl_url = f"{self._base_url}/api/v4/files/{fid}"
                async with self._session.get(
                    dl_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        file_data = await resp.read()
                        from gateway.platforms.base import cache_image_from_bytes, cache_document_from_bytes
                        if mime.startswith("image/"):
                            local_path = cache_image_from_bytes(file_data, ext or ".png")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        elif mime.startswith("audio/"):
                            from gateway.platforms.base import cache_audio_from_bytes
                            local_path = cache_audio_from_bytes(file_data, ext or ".ogg")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        else:
                            local_path = cache_document_from_bytes(file_data, fname)
                            media_urls.append(local_path)
                            media_types.append(mime)
                    else:
                        logger.warning("Mattermost: failed to download file %s: HTTP %s", fid, resp.status)
            except Exception as exc:
                logger.warning("Mattermost: error downloading file %s: %s", fid, exc)

        # Set message type based on downloaded media types.
        if media_types and msg_type == MessageType.TEXT:
            if any(m.startswith("image/") for m in media_types):
                msg_type = MessageType.PHOTO
            elif any(m.startswith("audio/") for m in media_types):
                msg_type = MessageType.VOICE
            elif media_types:
                msg_type = MessageType.DOCUMENT

        source = self.build_source(
            chat_id=channel_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
            thread_id=thread_id,
        )

        # Per-channel ephemeral prompt
        from gateway.platforms.base import resolve_channel_prompt
        _channel_prompt = resolve_channel_prompt(
            self.config.extra, channel_id, None,
        )

        msg_event = MessageEvent(
            text=message_text,
            message_type=msg_type,
            source=source,
            raw_message=post,
            message_id=post_id,
            media_urls=media_urls if media_urls else None,
            media_types=media_types if media_types else None,
            channel_prompt=_channel_prompt,
        )

        await self.handle_message(msg_event)




# ---------------------------------------------------------------------------
# Plugin standalone-send (out-of-process cron delivery via Mattermost REST)
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send via the Mattermost v4 REST API without a live gateway adapter.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (typical for cron jobs running out-of-process).
    Reads ``MATTERMOST_TOKEN`` from ``pconfig.token`` (set by the gateway
    config loader from env) and falls back to the ``MATTERMOST_TOKEN`` env
    var.  Server URL comes from ``pconfig.extra["url"]`` (set by the YAML
    bridge / env loader) or the ``MATTERMOST_URL`` env var.

    Thread replies (Mattermost CRT) are supported via the ``root_id`` field
    on the ``POST /posts`` payload — pass ``thread_id`` when threading is
    desired.  ``media_files`` are uploaded via ``POST /files``
    (multipart/form-data), then their returned ``file_id`` values are
    attached to the post.

    ``force_document`` is accepted for signature parity with other
    standalone senders but unused — Mattermost stores every uploaded file
    as a generic attachment regardless.
    """
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url = (
        (getattr(pconfig, "extra", {}) or {}).get("url")
        or os.getenv("MATTERMOST_URL", "")
    ).rstrip("/")
    token = (getattr(pconfig, "token", None) or os.getenv("MATTERMOST_TOKEN", "")).strip()
    if not base_url or not token:
        return {
            "error": (
                "Mattermost standalone send: MATTERMOST_URL and "
                "MATTERMOST_TOKEN must both be set"
            )
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    upload_headers = {"Authorization": f"Bearer {token}"}

    media_files = media_files or []

    try:
        # Resolve proxy + session kwargs once so a single ClientSession can
        # cover the optional file uploads + final post.
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url(platform_env_var="MATTERMOST_PROXY")
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            **_sess_kw,
        ) as session:
            # 1. Upload media (if any) and collect file_ids.
            file_ids: List[str] = []
            for media in media_files:
                file_path = media.get("path") if isinstance(media, dict) else media
                if not file_path or not os.path.exists(file_path):
                    continue
                form = aiohttp.FormData()
                # Mattermost requires channel_id on file uploads so the
                # server can attribute them.
                form.add_field("channel_id", chat_id)
                with open(file_path, "rb") as fh:
                    form.add_field(
                        "files",
                        fh.read(),
                        filename=os.path.basename(file_path),
                    )
                async with session.post(
                    f"{base_url}/api/v4/files",
                    data=form,
                    headers=upload_headers,
                    **_req_kw,
                ) as upload_resp:
                    if upload_resp.status not in {200, 201}:
                        body = await upload_resp.text()
                        return {
                            "error": (
                                f"Mattermost file upload failed "
                                f"({upload_resp.status}): {body[:400]}"
                            )
                        }
                    upload_data = await upload_resp.json()
                    for info in upload_data.get("file_infos", []):
                        if info.get("id"):
                            file_ids.append(info["id"])

            # 2. Post the message (with thread root + attached file_ids).
            payload: Dict[str, Any] = {
                "channel_id": chat_id,
                "message": message,
            }
            if thread_id:
                payload["root_id"] = thread_id
            if file_ids:
                payload["file_ids"] = file_ids
            async with session.post(
                f"{base_url}/api/v4/posts",
                headers=headers,
                json=payload,
                **_req_kw,
            ) as resp:
                if resp.status not in {200, 201}:
                    body = await resp.text()
                    return {
                        "error": (
                            f"Mattermost API error ({resp.status}): "
                            f"{body[:400]}"
                        )
                    }
                data = await resp.json()
            return {
                "success": True,
                "platform": "mattermost",
                "chat_id": chat_id,
                "message_id": data.get("id"),
            }
    except aiohttp.ClientError as exc:
        return {"error": f"Mattermost send failed (network): {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Mattermost send failed: {exc}"}


# ---------------------------------------------------------------------------
# Interactive setup wizard
# ---------------------------------------------------------------------------


def interactive_setup() -> None:
    """Guide the user through Mattermost bot setup.

    Mirrors Discord/Teams' ``interactive_setup`` shape: lazy-imports CLI
    helpers so the plugin's import surface stays small, prompts for the
    server URL + bot token, captures an allowlist, and offers to set a
    home channel.  Replaces the central
    ``hermes_cli/setup.py::_setup_mattermost`` function this migration
    removes.
    """
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_header,
        print_info,
        print_success,
    )

    print_header("Mattermost")
    existing = get_env_value("MATTERMOST_TOKEN")
    if existing:
        print_info("Mattermost: already configured")
        if not prompt_yes_no("Reconfigure Mattermost?", False):
            return

    print_info("Works with any self-hosted Mattermost instance.")
    print_info("   1. In Mattermost: Integrations → Bot Accounts → Add Bot Account")
    print_info("   2. Copy the bot token")
    print()
    mm_url = prompt("Mattermost server URL (e.g. https://mm.example.com)")
    if mm_url:
        save_env_value("MATTERMOST_URL", mm_url.rstrip("/"))
    token = prompt("Bot token", password=True)
    if not token:
        return
    save_env_value("MATTERMOST_TOKEN", token)
    print_success("Mattermost token saved")

    print()
    print_info("🔒 Security: Restrict who can use your bot")
    print_info("   To find your user ID: click your avatar → Profile")
    print_info("   or use the API: GET /api/v4/users/me")
    print()
    allowed_users = prompt("Allowed user IDs (comma-separated, leave empty for open access)")
    if allowed_users:
        save_env_value("MATTERMOST_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Mattermost allowlist configured")
    else:
        print_info("⚠️  No allowlist set - anyone who can message the bot can use it!")

    print()
    print_info("📬 Home Channel: where Hermes delivers cron job results and notifications.")
    print_info("   To get a channel ID: click channel name → View Info → copy the ID")
    print_info("   You can also set this later by typing /set-home in a Mattermost channel.")
    home_channel = prompt("Home channel ID (leave empty to set later with /set-home)")
    if home_channel:
        save_env_value("MATTERMOST_HOME_CHANNEL", home_channel)
    print_info("   Open config in your editor:  hermes config edit")


# ---------------------------------------------------------------------------
# YAML → env config bridge (apply_yaml_config_fn, #25443)
# ---------------------------------------------------------------------------


def _apply_yaml_config(yaml_cfg: dict, mattermost_cfg: dict) -> dict | None:
    """Translate ``config.yaml`` ``mattermost:`` keys into env vars.

    Implements the ``apply_yaml_config_fn`` contract (#24836 / #25443).
    Mirrors the legacy ``mattermost_cfg`` block that used to live in
    ``gateway/config.py::load_gateway_config()`` before this migration.

    The MattermostAdapter reads its runtime configuration via
    ``os.getenv()`` for ``MATTERMOST_REQUIRE_MENTION``,
    ``MATTERMOST_FREE_RESPONSE_CHANNELS``, and
    ``MATTERMOST_ALLOWED_CHANNELS``.  Rather than rewrite those call sites
    to read from ``PlatformConfig.extra``, this hook keeps the env-driven
    model and merely owns the YAML→env translation here, next to the
    adapter that consumes it.

    Env vars take precedence over YAML — every assignment is guarded
    by ``not os.getenv(...)`` so an explicit env var survives a config.yaml
    update.  Returns ``None`` because no extras are seeded into
    ``PlatformConfig.extra`` directly (everything flows through env).
    """
    if "require_mention" in mattermost_cfg and not os.getenv("MATTERMOST_REQUIRE_MENTION"):
        os.environ["MATTERMOST_REQUIRE_MENTION"] = str(mattermost_cfg["require_mention"]).lower()
    frc = mattermost_cfg.get("free_response_channels")
    if frc is not None and not os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["MATTERMOST_FREE_RESPONSE_CHANNELS"] = str(frc)
    # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
    ac = mattermost_cfg.get("allowed_channels")
    if ac is not None and not os.getenv("MATTERMOST_ALLOWED_CHANNELS"):
        if isinstance(ac, list):
            ac = ",".join(str(v) for v in ac)
        os.environ["MATTERMOST_ALLOWED_CHANNELS"] = str(ac)
    return None  # all settings flow through env; nothing to merge into extras


# ---------------------------------------------------------------------------
# is_connected probe
# ---------------------------------------------------------------------------


def _is_connected(config) -> bool:
    """Mattermost is considered connected when BOTH MATTERMOST_TOKEN and
    MATTERMOST_URL are set.

    Looks up via ``hermes_cli.gateway.get_env_value`` at call time (not via
    the plugin's own bound import) so tests that patch
    ``gateway_mod.get_env_value`` can suppress ambient env vars.  Matches
    what the legacy connected-platforms check did before this migration.
    """
    import hermes_cli.gateway as gateway_mod
    return bool(
        (gateway_mod.get_env_value("MATTERMOST_TOKEN") or "").strip()
        and (gateway_mod.get_env_value("MATTERMOST_URL") or "").strip()
    )


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------


def _build_adapter(config):
    """Factory wrapper that constructs MattermostAdapter from a PlatformConfig."""
    return MattermostAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="mattermost",
        label="Mattermost",
        adapter_factory=_build_adapter,
        check_fn=check_mattermost_requirements,
        is_connected=_is_connected,
        required_env=["MATTERMOST_URL", "MATTERMOST_TOKEN"],
        install_hint="pip install aiohttp",
        # Interactive setup wizard — replaces the central
        # hermes_cli/setup.py::_setup_mattermost function.
        setup_fn=interactive_setup,
        # YAML→env config bridge — owns the translation of
        # ``config.yaml`` ``mattermost:`` keys (require_mention,
        # free_response_channels, allowed_channels) into ``MATTERMOST_*``
        # env vars that the adapter reads via ``os.getenv()``.  Replaces
        # the hardcoded block that used to live in ``gateway/config.py``.
        # Hook contract: #24836 / #25443.
        apply_yaml_config_fn=_apply_yaml_config,
        # Auth env vars for _is_user_authorized() integration.
        allowed_users_env="MATTERMOST_ALLOWED_USERS",
        allow_all_env="MATTERMOST_ALLOW_ALL_USERS",
        # Cron home-channel delivery.
        cron_deliver_env_var="MATTERMOST_HOME_CHANNEL",
        # Out-of-process cron delivery via Mattermost REST API.  Without
        # this hook, ``deliver=mattermost`` cron jobs fail with "No live
        # adapter" when cron runs separately from the gateway.  Mirrors
        # the Discord / Teams pattern.
        standalone_sender_fn=_standalone_send,
        # Mattermost practical post-length limit (server default is 16383
        # but 4000 is the readable threshold the adapter has used since
        # day one).
        max_message_length=MAX_POST_LENGTH,
        # Display
        emoji="💬",
        allow_update_command=True,
    )
