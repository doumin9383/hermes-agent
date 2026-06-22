"""Tests for Mattermost platform adapter."""
import asyncio
import json
import os
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.run import (
    _resolve_gateway_display_bool,
    _resolve_progress_thread_id,
)


class TestMattermostProgressThreadRouting:
    def test_top_level_mattermost_progress_uses_event_message_id(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id=None,
            event_message_id="top_post_123",
        ) == "top_post_123"

    def test_threaded_mattermost_progress_prefers_existing_thread_root(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id="root_post_123",
            event_message_id="reply_post_456",
        ) == "root_post_123"

    def test_telegram_progress_does_not_use_message_id_as_thread_id(self):
        assert _resolve_progress_thread_id(
            Platform.TELEGRAM,
            source_thread_id=None,
            event_message_id="12345",
        ) is None


class TestMattermostDisplayHygiene:
    def test_mattermost_requires_platform_opt_in_for_interim_assistant_messages(self):
        """Global interim commentary must not make Mattermost leak scratch notes."""
        user_config = {"display": {"interim_assistant_messages": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_platform_opt_in_can_enable_interim_assistant_messages(self):
        """Mattermost can still opt into commentary explicitly per platform."""
        user_config = {
            "display": {
                "interim_assistant_messages": False,
                "platforms": {
                    "mattermost": {"interim_assistant_messages": True},
                },
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True

    def test_mattermost_requires_platform_opt_in_for_thinking_progress(self):
        """Global thinking_progress must not surface internal analysis in Mattermost."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "thinking_progress",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_requires_platform_opt_in_for_show_reasoning(self):
        """Global show_reasoning must not prepend scratch reasoning in Mattermost."""
        user_config = {"display": {"show_reasoning": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "show_reasoning",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_platform_opt_in_can_enable_show_reasoning(self):
        user_config = {
            "display": {
                "show_reasoning": False,
                "platforms": {"mattermost": {"show_reasoning": True}},
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "show_reasoning",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True

    def test_global_thinking_progress_still_applies_to_other_platforms(self):
        """The Mattermost guard must not silently neuter Telegram/other chats."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "telegram",
            "thinking_progress",
            default=False,
            platform=Platform.TELEGRAM,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestMattermostConfigLoading:
    def test_apply_env_overrides_mattermost(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST in config.platforms
        mc = config.platforms[Platform.MATTERMOST]
        assert mc.enabled is True
        assert mc.token == "mm-tok-abc123"
        assert mc.extra.get("url") == "https://mm.example.com"

    def test_mattermost_not_loaded_without_token(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST not in config.platforms

    def test_mattermost_home_channel(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL", "ch_abc123")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL_NAME", "General")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.get_home_channel(Platform.MATTERMOST)
        assert home is not None
        assert home.chat_id == "ch_abc123"
        assert home.name == "General"

    def test_mattermost_url_warning_without_url(self, monkeypatch):
        """MATTERMOST_TOKEN set but MATTERMOST_URL missing should still load."""
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.delenv("MATTERMOST_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST in config.platforms
        assert config.platforms[Platform.MATTERMOST].extra.get("url") == ""


# ---------------------------------------------------------------------------
# Adapter format / truncate
# ---------------------------------------------------------------------------

def _make_adapter():
    """Create a MattermostAdapter with mocked config."""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"url": "https://mm.example.com"},
    )
    adapter = MattermostAdapter(config)
    return adapter


class TestMattermostFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_to_url(self):
        """![alt](url) should be converted to just the URL."""
        result = self.adapter.format_message("![cat](https://img.example.com/cat.png)")
        assert result == "https://img.example.com/cat.png"

    def test_image_markdown_strips_alt_text(self):
        result = self.adapter.format_message("Here: ![my image](https://x.com/a.jpg) done")
        assert "![" not in result
        assert "https://x.com/a.jpg" in result

    def test_regular_markdown_preserved(self):
        """Regular markdown (bold, italic, code) should be kept as-is."""
        content = "**bold** and *italic* and `code`"
        assert self.adapter.format_message(content) == content

    def test_regular_links_preserved(self):
        """Non-image links should be preserved."""
        content = "[click](https://example.com)"
        assert self.adapter.format_message(content) == content

    def test_plain_text_unchanged(self):
        content = "Hello, world!"
        assert self.adapter.format_message(content) == content

    def test_multiple_images(self):
        content = "![a](http://a.com/1.png) text ![b](http://b.com/2.png)"
        result = self.adapter.format_message(content)
        assert "![" not in result
        assert "http://a.com/1.png" in result
        assert "http://b.com/2.png" in result


class TestMattermostTruncateMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_short_message_single_chunk(self):
        msg = "Hello, world!"
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) == 1
        assert chunks[0] == msg

    def test_long_message_splits(self):
        msg = "a " * 2500  # 5000 chars
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_custom_max_length(self):
        msg = "Hello " * 20
        chunks = self.adapter.truncate_message(msg, max_length=50)
        assert all(len(c) <= 50 for c in chunks)

    def test_exactly_at_limit(self):
        msg = "x" * 4000
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestMattermostSend:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    async def test_send_calls_api_post(self):
        """send() should POST to /api/v4/posts with channel_id and message."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post123"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is True
        assert result.message_id == "post123"

        # Verify post was called with correct URL
        call_args = self.adapter._session.post.call_args
        assert "/api/v4/posts" in call_args[0][0]
        # Verify payload
        payload = call_args[1]["json"]
        assert payload["channel_id"] == "channel_1"
        assert payload["message"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_empty_content_succeeds(self):
        """Empty content should return success without calling the API."""
        result = await self.adapter.send("channel_1", "")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_with_thread_reply(self):
        """When reply_mode is 'thread', reply_to should become root_id."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post456"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        # send() now calls _resolve_root_id first
        mock_get_resp = AsyncMock()
        mock_get_resp.status = 200
        mock_get_resp.json = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        mock_get_resp.text = AsyncMock(return_value="")
        mock_get_resp.__aenter__ = AsyncMock(return_value=mock_get_resp)
        mock_get_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)
        self.adapter._session.get = MagicMock(return_value=mock_get_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_post"

    @pytest.mark.asyncio
    async def test_send_with_metadata_thread_id(self):
        """When reply_mode is 'thread', metadata.thread_id should become root_id."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post_meta"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send(
            "channel_1", "Reply!", metadata={"thread_id": "root_from_metadata"}
        )

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_from_metadata"

    @pytest.mark.asyncio
    async def test_send_metadata_thread_id_takes_precedence_over_reply_to(self):
        """metadata.thread_id should remain the root when reply_to points at a child reply."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post_precedence"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send(
            "channel_1",
            "Reply!",
            reply_to="root_from_reply",
            metadata={"thread_id": "root_from_metadata"},
        )

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_from_metadata"

    @pytest.mark.asyncio
    async def test_send_document_with_metadata_thread_id(self, tmp_path):
        """File posts should also route metadata.thread_id to Mattermost root_id."""
        self.adapter._reply_mode = "thread"
        file_path = tmp_path / "result.txt"
        file_path.write_text("skill output", encoding="utf-8")

        self.adapter._upload_file = AsyncMock(return_value="file_123")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post_file"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send_document(
            "channel_1",
            str(file_path),
            caption="result",
            metadata={"thread_id": "root_from_metadata"},
        )

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_from_metadata"
        assert payload["file_ids"] == ["file_123"]

    @pytest.mark.asyncio
    async def test_send_multiple_images_with_metadata_thread_id(self, tmp_path):
        """Batched image posts should route metadata.thread_id to Mattermost root_id."""
        self.adapter._reply_mode = "thread"
        image_path = tmp_path / "result.png"
        image_path.write_bytes(b"png")

        self.adapter._upload_file = AsyncMock(return_value="file_img")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post_images"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session.post = MagicMock(return_value=mock_resp)

        await self.adapter.send_multiple_images(
            "channel_1",
            [(f"file://{image_path}", "result")],
            metadata={"thread_id": "root_from_metadata"},
        )

        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_from_metadata"
        assert payload["file_ids"] == ["file_img"]

    @pytest.mark.asyncio
    async def test_send_without_thread_no_root_id(self):
        """When reply_mode is 'off', reply_to should NOT set root_id."""
        self.adapter._reply_mode = "off"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post789"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert "root_id" not in payload


    @pytest.mark.asyncio
    async def test_send_uses_metadata_thread_id_for_progress_messages(self):
        """Progress/status messages pass Mattermost thread context via metadata."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "root_post_123", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={"id": "progress_post"})

        result = await self.adapter.send(
            "channel_1",
            "⚡ terminal...",
            metadata={"thread_id": "root_post_123"},
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "root_post_123"

    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_notify_send_with_invalid_thread_root_falls_back_flat_with_warning(self):
        """Notify-worthy replies may fall back flat so the answer is not lost."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(side_effect=[{}, {"id": "flat_final"}])

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="bad_root",
            metadata={"notify": True},
        )

        assert result.success is True
        assert result.message_id == "flat_final"
        assert self.adapter._api_post.call_count == 2
        threaded_payload = self.adapter._api_post.call_args_list[0][0][1]
        flat_payload = self.adapter._api_post.call_args_list[1][0][1]
        assert threaded_payload["root_id"] == "bad_root"
        assert "root_id" not in flat_payload
        assert flat_payload["channel_id"] == "channel_1"
        assert "Mattermost thread delivery failed" in flat_payload["message"]
        assert "Final answer body" in flat_payload["message"]

    @pytest.mark.asyncio
    async def test_notify_send_with_server_error_does_not_fall_back_flat(self):
        """Notify fallback is only for broken thread roots, not generic API failures."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        self.adapter._last_post_status = 500
        self.adapter._last_post_error = "Internal Server Error"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="root_post",
            metadata={"notify": True},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "root_post"

    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_send_api_failure(self):
        """When API returns error, send should return failure."""
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.json = AsyncMock(return_value={})
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is False


# ---------------------------------------------------------------------------
# WebSocket event parsing
# ---------------------------------------------------------------------------

class TestMattermostWebSocketParsing:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        # Mock handle_message to capture the MessageEvent without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_posted_event(self):
        """'posted' events should extract message from double-encoded post JSON."""
        post_data = {
            "id": "post_abc",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello from Matrix!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),  # double-encoded JSON string
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        # @mention is stripped from the message text
        assert msg_event.text == "Hello from Matrix!"
        assert msg_event.message_id == "post_abc"

    @pytest.mark.asyncio
    async def test_ignore_own_messages(self):
        """Messages from the bot's own user_id should be ignored."""
        post_data = {
            "id": "post_self",
            "user_id": "bot_user_id",  # same as bot
            "channel_id": "chan_456",
            "message": "Bot echo",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_ignore_non_posted_events(self):
        """Non-'posted' events should be ignored."""
        event = {
            "event": "typing",
            "data": {"user_id": "user_123"},
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_ignore_system_posts(self):
        """Posts with a 'type' field (system messages) should be ignored."""
        post_data = {
            "id": "sys_post",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "user joined",
            "type": "system_join_channel",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_channel_type_mapping(self):
        """channel_type 'D' should map to 'dm'."""
        post_data = {
            "id": "post_dm",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": "DM message",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_thread_id_from_root_id(self):
        """Post with root_id should have thread_id set."""
        post_data = {
            "id": "post_reply",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Thread reply",
            "root_id": "root_post_123",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.thread_id == "root_post_123"

    @pytest.mark.asyncio
    async def test_invalid_post_json_ignored(self):
        """Invalid JSON in data.post should be silently ignored."""
        event = {
            "event": "posted",
            "data": {
                "post": "not-valid-json{{{",
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called


# ---------------------------------------------------------------------------
# Mention behavior (require_mention + free_response_channels)
# ---------------------------------------------------------------------------

class TestMattermostMentionBehavior:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        self.adapter.handle_message = AsyncMock()

    def _make_event(
        self,
        message,
        channel_type="O",
        channel_id="chan_456",
        post_id="post_mention",
        root_id=None,
    ):
        post_data = {
            "id": post_id,
            "user_id": "user_123",
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            post_data["root_id"] = root_id
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": channel_type,
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_require_mention_true_skips_without_mention(self):
        """Default: messages without @mention in channels are skipped."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_require_mention_false_responds_to_all(self):
        """MATTERMOST_REQUIRE_MENTION=false: respond to all channel messages."""
        with patch.dict(os.environ, {"MATTERMOST_REQUIRE_MENTION": "false"}):
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_free_response_channel_responds_without_mention(self):
        """Messages in free-response channels don't need @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_456,chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_non_free_channel_still_requires_mention(self):
        """Channels NOT in free-response list still require @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_free_response_top_level_message_becomes_thread_root(self):
        self.adapter._reply_mode = "thread"
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_456"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(
                self._make_event("start work", post_id="root_post_456")
            )
            assert self.adapter.handle_message.called
            msg = self.adapter.handle_message.call_args[0][0]
            assert msg.source.thread_id == "root_post_456"

    @pytest.mark.asyncio
    async def test_thread_reply_missing_root_id_is_resolved_before_mention_gate(self):
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(
            side_effect=[
                {"root_id": "root_post_456"},
                {"message": "@hermes-bot start work", "user_id": "user_123"},
            ]
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(
                self._make_event("continue", post_id="reply_1")
            )
            assert self.adapter.handle_message.called
            msg = self.adapter.handle_message.call_args[0][0]
            assert msg.source.thread_id == "root_post_456"

    @pytest.mark.asyncio
    async def test_thread_followup_allowed_when_root_mentioned_bot(self):
        self.adapter._api_get = AsyncMock(
            return_value={"message": "@hermes-bot start work", "user_id": "user_123"}
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(
                self._make_event("continue", post_id="reply_1", root_id="root_post_456")
            )
            assert self.adapter.handle_message.called
            msg = self.adapter.handle_message.call_args[0][0]
            assert msg.source.thread_id == "root_post_456"

    @pytest.mark.asyncio
    async def test_allowed_channels_blocks_other_channels_even_with_mention(self):
        with patch.dict(os.environ, {"MATTERMOST_ALLOWED_CHANNELS": "chan_allowed"}):
            await self.adapter._handle_ws_event(
                self._make_event("@hermes-bot hello", channel_id="chan_blocked")
            )
            assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dm_always_responds(self):
        """DMs (channel_type=D) always respond regardless of mention settings."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_type="D"))
            assert self.adapter.handle_message.called

    def test_metadata_thread_id_takes_precedence_over_reply_to(self):
        self.adapter._reply_mode = "thread"
        root_id = self.adapter._thread_root_id(
            reply_to="reply_post_123",
            metadata={"thread_id": "root_post_456"},
        )
        assert root_id == "root_post_456"

    @pytest.mark.asyncio
    async def test_mention_stripped_from_text(self):
        """@mention is stripped from message text."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(
                self._make_event("@hermes-bot what is 2+2")
            )
            assert self.adapter.handle_message.called
            msg = self.adapter.handle_message.call_args[0][0]
            assert "@hermes-bot" not in msg.text
            assert "2+2" in msg.text


# ---------------------------------------------------------------------------
# File upload (send_image)
# ---------------------------------------------------------------------------

class TestMattermostFileUpload:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_send_image_downloads_and_uploads(self, _mock_safe):
        """send_image should download the URL, upload via /api/v4/files, then post."""
        # Mock the download (GET)
        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.read = AsyncMock(return_value=b"\x89PNG\x00fake-image-data")
        mock_dl_resp.content_type = "image/png"
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the upload (POST to /files)
        mock_upload_resp = AsyncMock()
        mock_upload_resp.status = 200
        mock_upload_resp.json = AsyncMock(return_value={
            "file_infos": [{"id": "file_abc123"}]
        })
        mock_upload_resp.text = AsyncMock(return_value="")
        mock_upload_resp.__aenter__ = AsyncMock(return_value=mock_upload_resp)
        mock_upload_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the post (POST to /posts)
        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"id": "post_with_file"})
        mock_post_resp.text = AsyncMock(return_value="")
        mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
        mock_post_resp.__aexit__ = AsyncMock(return_value=False)

        # Route calls: first GET (download), then POST (upload), then POST (create post)
        self.adapter._session.get = MagicMock(return_value=mock_dl_resp)
        post_call_count = 0
        original_post_returns = [mock_upload_resp, mock_post_resp]

        def post_side_effect(*args, **kwargs):
            nonlocal post_call_count
            resp = original_post_returns[min(post_call_count, len(original_post_returns) - 1)]
            post_call_count += 1
            return resp

        self.adapter._session.post = MagicMock(side_effect=post_side_effect)

        result = await self.adapter.send_image(
            "channel_1", "https://img.example.com/cat.png", caption="A cat"
        )

        assert result.success is True
        assert result.message_id == "post_with_file"


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

class TestMattermostDedup:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        # Mock handle_message to capture calls without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_duplicate_post_ignored(self):
        """The same post_id within the TTL window should be ignored."""
        post_data = {
            "id": "post_dup",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        # First time: should process
        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.call_count == 1

        # Second time (same post_id): should be deduped
        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.call_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_different_post_ids_both_processed(self):
        """Different post IDs should both be processed."""
        for i, pid in enumerate(["post_a", "post_b"]):
            post_data = {
                "id": pid,
                "user_id": "user_123",
                "channel_id": "chan_456",
                "message": f"@bot_user_id Message {i}",
            }
            event = {
                "event": "posted",
                "data": {
                    "post": json.dumps(post_data),
                    "channel_type": "O",
                    "sender_name": "@alice",
                },
            }
            await self.adapter._handle_ws_event(event)

        assert self.adapter.handle_message.call_count == 2

    def test_prune_seen_clears_expired(self):
        """Dedup cache should remove entries older than TTL on overflow."""
        now = time.time()
        dedup = self.adapter._dedup
        # Fill with enough expired entries to trigger pruning
        for i in range(dedup._max_size + 10):
            dedup._seen[f"old_{i}"] = now - 600  # 10 min ago (older than default TTL)

        # Add a fresh one
        dedup._seen["fresh"] = now

        # Trigger pruning by calling is_duplicate with a new entry (over max_size)
        dedup.is_duplicate("trigger_prune")

        # Old entries should be pruned, fresh one kept
        assert "fresh" in dedup._seen
        assert len(dedup._seen) < dedup._max_size + 10

    def test_seen_cache_tracks_post_ids(self):
        """Posts are tracked in the dedup cache."""
        self.adapter._dedup._seen["test_post"] = time.time()
        assert "test_post" in self.adapter._dedup._seen


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

class TestMattermostRequirements:
    def test_check_requirements_with_token_and_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is True

    def test_check_requirements_without_token(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is False

    def test_check_requirements_without_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is False


# ---------------------------------------------------------------------------
# Media type propagation (MIME types, not bare strings)
# ---------------------------------------------------------------------------

class TestMattermostMediaTypes:
    """Verify that media_types contains actual MIME types (e.g. 'image/png')
    rather than bare category strings ('image'), so downstream
    ``mtype.startswith("image/")`` checks in run.py work correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, file_ids):
        post_data = {
            "id": "post_media",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id file attached",
            "file_ids": file_ids,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_image_media_type_is_full_mime(self):
        """An image attachment should produce 'image/png', not 'image'."""
        file_info = {"name": "photo.png", "mime_type": "image/png"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"\x89PNG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/tmp/photo.png"):
            await self.adapter._handle_ws_event(self._make_event(["file1"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["image/png"]
        assert msg.media_types[0].startswith("image/")

    @pytest.mark.asyncio
    async def test_audio_media_type_is_full_mime(self):
        """An audio attachment should produce 'audio/ogg', not 'audio'."""
        file_info = {"name": "voice.ogg", "mime_type": "audio/ogg"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"OGG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_audio_from_bytes", return_value="/tmp/voice.ogg"), \
             patch("gateway.platforms.base.cache_image_from_bytes"), \
             patch("gateway.platforms.base.cache_document_from_bytes"):
            await self.adapter._handle_ws_event(self._make_event(["file2"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["audio/ogg"]
        assert msg.media_types[0].startswith("audio/")

    @pytest.mark.asyncio
    async def test_document_media_type_is_full_mime(self):
        """A document attachment should produce 'application/pdf', not 'document'."""
        file_info = {"name": "report.pdf", "mime_type": "application/pdf"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"PDF fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_document_from_bytes", return_value="/tmp/report.pdf"), \
             patch("gateway.platforms.base.cache_image_from_bytes"):
            await self.adapter._handle_ws_event(self._make_event(["file3"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["application/pdf"]
        assert not msg.media_types[0].startswith("image/")
        assert not msg.media_types[0].startswith("audio/")





# ---------------------------------------------------------------------------
# _thread_root_id — edge cases
# ---------------------------------------------------------------------------

class TestMattermostThreadRootId:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_reply_mode_off_returns_none_with_reply_to(self):
        """When reply_mode is 'off', _thread_root_id returns None."""
        self.adapter._reply_mode = "off"
        assert self.adapter._thread_root_id(reply_to="post_123") is None

    def test_reply_mode_off_returns_none_with_metadata(self):
        self.adapter._reply_mode = "off"
        assert self.adapter._thread_root_id(metadata={"thread_id": "root_123"}) is None

    def test_reply_mode_thread_without_reply_to_or_metadata(self):
        """When reply_mode is 'thread' but no args, returns None."""
        self.adapter._reply_mode = "thread"
        assert self.adapter._thread_root_id() is None

    def test_thread_root_id_only_reply_to(self):
        """reply_to used when there's no metadata."""
        self.adapter._reply_mode = "thread"
        result = self.adapter._thread_root_id(reply_to="reply_abc")
        assert result == "reply_abc"

    def test_thread_root_id_only_metadata_thread_id(self):
        """metadata.thread_id used when there's no reply_to."""
        self.adapter._reply_mode = "thread"
        result = self.adapter._thread_root_id(metadata={"thread_id": "meta_root"})
        assert result == "meta_root"

    def test_thread_root_id_metadata_takes_precedence(self):
        """metadata.thread_id wins over reply_to."""
        self.adapter._reply_mode = "thread"
        result = self.adapter._thread_root_id(
            reply_to="reply_post",
            metadata={"thread_id": "meta_root"},
        )
        assert result == "meta_root"

    def test_thread_root_id_empty_thread_id_falls_through(self):
        """metadata with thread_id='' falls through to reply_to."""
        self.adapter._reply_mode = "thread"
        result = self.adapter._thread_root_id(
            reply_to="reply_abc",
            metadata={"thread_id": ""},
        )
        assert result == "reply_abc"

    def test_thread_root_id_empty_metadata_dict_falls_through(self):
        """metadata={} falls through to reply_to."""
        self.adapter._reply_mode = "thread"
        result = self.adapter._thread_root_id(
            reply_to="reply_abc",
            metadata={},
        )
        assert result == "reply_abc"


# ---------------------------------------------------------------------------
# send_typing — thread parent_id support
# ---------------------------------------------------------------------------

class TestMattermostSendTyping:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_u_123"
        self.adapter._api_post = AsyncMock(return_value={})

    @pytest.mark.asyncio
    async def test_send_typing_basic(self):
        """send_typing should POST to users/{id}/typing with channel_id."""
        await self.adapter.send_typing("chan_1")
        self.adapter._api_post.assert_called_once()
        path = self.adapter._api_post.call_args[0][0]
        payload = self.adapter._api_post.call_args[0][1]
        assert "typing" in path
        assert payload["channel_id"] == "chan_1"

    @pytest.mark.asyncio
    async def test_send_typing_with_thread_id_sets_parent_id(self):
        """When metadata has thread_id, it should be passed as parent_id."""
        await self.adapter.send_typing("chan_1", metadata={"thread_id": "thread_root_123"})
        payload = self.adapter._api_post.call_args[0][1]
        assert payload["parent_id"] == "thread_root_123"

    @pytest.mark.asyncio
    async def test_send_typing_without_thread_id_no_parent_id(self):
        """Without thread_id metadata, parent_id should not be in payload."""
        await self.adapter.send_typing("chan_1")
        payload = self.adapter._api_post.call_args[0][1]
        assert "parent_id" not in payload

    @pytest.mark.asyncio
    async def test_send_typing_correct_endpoint(self):
        """The API endpoint should include the bot user ID."""
        await self.adapter.send_typing("chan_1")
        path = self.adapter._api_post.call_args[0][0]
        assert f"users/{self.adapter._bot_user_id}/typing" in path


# ---------------------------------------------------------------------------
# Interactive message sending (buttons via Message Attachments)
# ---------------------------------------------------------------------------

class _InteractiveTestBase:
    """Shared mocks for interactive message tests."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._api_post = AsyncMock()
        self.adapter._callback_runner = MagicMock()  # non-None = server is running

    def _assert_post_payload(self, chat_id, has_root_id=False, root_id_val="root_meta"):
        """Verify the _api_post call had the right structure."""
        self.adapter._api_post.assert_called_once()
        path, payload = self.adapter._api_post.call_args[0]
        assert "posts" in path
        assert payload["channel_id"] == chat_id
        if has_root_id:
            assert payload["root_id"] == root_id_val
        return payload


class TestMattermostSendInteractivePost(_InteractiveTestBase):
    @pytest.mark.asyncio
    async def test_send_interactive_basic(self):
        """_send_interactive_post sends attachments under props."""
        self.adapter._api_post = AsyncMock(return_value={"id": "post_int"})
        attachments = [{"title": "Test", "actions": []}]
        result = await self.adapter._send_interactive_post(
            "chan_1", "Hello", attachments,
        )
        assert result.success is True
        assert result.message_id == "post_int"
        payload = self.adapter._api_post.call_args[0][1]
        assert payload["props"]["attachments"] == attachments
        assert "attachments" not in payload
        assert payload["message"] == "Hello"

    @pytest.mark.asyncio
    async def test_send_interactive_with_root_id(self):
        """_send_interactive_post passes root_id when metadata.thread_id is set."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_post = AsyncMock(return_value={"id": "post_int2"})
        result = await self.adapter._send_interactive_post(
            "chan_1", "Hello", [],
            metadata={"thread_id": "root_meta"},
        )
        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        assert payload["root_id"] == "root_meta"

    @pytest.mark.asyncio
    async def test_send_interactive_api_failure(self):
        """When API returns empty dict, send returns failure."""
        self.adapter._api_post = AsyncMock(return_value={})
        result = await self.adapter._send_interactive_post(
            "chan_1", "Hello", [],
        )
        assert result.success is False


class TestMattermostSendExecApproval(_InteractiveTestBase):
    @pytest.mark.asyncio
    async def test_send_exec_approval_sends_buttons(self):
        """send_exec_approval sends approval message with four buttons."""
        self.adapter._api_post = AsyncMock(return_value={"id": "approval_post"})

        result = await self.adapter.send_exec_approval(
            chat_id="chan_1",
            command="rm -rf /important",
            session_key="agent:main:chan_1:123",
            description="dangerous command",
        )

        assert result.success is True
        assert result.message_id == "approval_post"
        payload = self.adapter._api_post.call_args[0][1]
        assert payload["channel_id"] == "chan_1"
        attachments = payload["props"]["attachments"]
        assert len(attachments) == 1
        assert "Command Approval" in attachments[0]["title"]
        actions = attachments[0]["actions"]
        assert len(actions) == 4
        labels = [a["name"] for a in actions]
        assert "Allow Once" in labels[0] or "✅ Allow Once" in labels[0]

    @pytest.mark.asyncio
    async def test_exec_approval_with_metadata_thread_id(self):
        """send_exec_approval respects metadata.thread_id."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_post = AsyncMock(return_value={"id": "approval_post2"})

        result = await self.adapter.send_exec_approval(
            chat_id="chan_1",
            command="rm -rf /important",
            session_key="agent:main:chan_1:123",
            metadata={"thread_id": "thread_root"},
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        assert payload["root_id"] == "thread_root"

    @pytest.mark.asyncio
    async def test_exec_approval_long_command_truncated(self):
        """Very long commands are truncated in the preview."""
        long_cmd = "x" * 5000
        self.adapter._api_post = AsyncMock(return_value={"id": "approval_post3"})

        result = await self.adapter.send_exec_approval(
            chat_id="chan_1",
            command=long_cmd,
            session_key="agent:main:chan_1:123",
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        text = payload["props"]["attachments"][0]["text"]
        assert len(text) < 4500
        assert "..." in text

    @pytest.mark.asyncio
    async def test_exec_approval_fallback_when_no_callback_server(self):
        """When callback server isn't running, returns failure."""
        self.adapter._callback_runner = None
        result = await self.adapter.send_exec_approval(
            chat_id="chan_1",
            command="ls",
            session_key="agent:main:chan_1:123",
        )
        assert result.success is False


class TestMattermostSendSlashConfirm(_InteractiveTestBase):
    @pytest.mark.asyncio
    async def test_send_slash_confirm_sends_buttons(self):
        """send_slash_confirm sends three buttons: Approve Once, Always, Cancel."""
        self.adapter._api_post = AsyncMock(return_value={"id": "confirm_post"})

        result = await self.adapter.send_slash_confirm(
            chat_id="chan_1",
            title="Run Command?",
            message="Are you sure?",
            session_key="agent:main:chan_1:123",
            confirm_id="confirm_abc",
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        actions = payload["props"]["attachments"][0]["actions"]
        assert len(actions) == 3

    @pytest.mark.asyncio
    async def test_slash_confirm_fallback(self):
        """Without callback server, returns failure."""
        self.adapter._callback_runner = None
        result = await self.adapter.send_slash_confirm(
            chat_id="chan_1",
            title="Test",
            message="Test",
            session_key="key",
            confirm_id="cid",
        )
        assert result.success is False


class TestMattermostSendClarify(_InteractiveTestBase):
    @pytest.mark.asyncio
    async def test_send_clarify_with_choices_sends_buttons(self):
        """send_clarify with options sends one button per choice plus 'Other'."""
        self.adapter._api_post = AsyncMock(return_value={"id": "clarify_post"})

        result = await self.adapter.send_clarify(
            chat_id="chan_1",
            question="Pick one?",
            choices=["A", "B", "C"],
            clarify_id="clarify_1",
            session_key="agent:main:chan_1:123",
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        actions = payload["props"]["attachments"][0]["actions"]
        # 3 choices + 1 "Other" = 4
        assert len(actions) == 4

    @pytest.mark.asyncio
    async def test_send_clarify_open_ended_falls_back(self):
        """send_clarify without choices falls back to base class (no attachments)."""
        from gateway.platforms.base import BasePlatformAdapter
        self.adapter._api_post = AsyncMock(return_value={"id": "clarify_post2"})
        # Stub the base class method to prove the fallback happens
        with patch.object(BasePlatformAdapter, "send_clarify", new=AsyncMock(return_value=MagicMock(success=True))):
            result = await self.adapter.send_clarify(
                chat_id="chan_1",
                question="What do you think?",
                choices=None,
                clarify_id="clarify_2",
                session_key="agent:main:chan_1:123",
            )
            assert result.success is True
            BasePlatformAdapter.send_clarify.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_clarify_fallback(self):
        """Without callback server, returns failure."""
        self.adapter._callback_runner = None
        result = await self.adapter.send_clarify(
            chat_id="chan_1",
            question="Q",
            choices=["A", "B"],
            clarify_id="cid",
            session_key="key",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_send_clarify_cleans_invalid_choices(self):
        """Invalid (None, empty) choices are filtered out."""
        self.adapter._api_post = AsyncMock(return_value={"id": "clarify_post3"})
        result = await self.adapter.send_clarify(
            chat_id="chan_1",
            question="Pick:",
            choices=["A", "B"],
            clarify_id="clarify_3",
            session_key="agent:main:chan_1:123",
        )
        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        actions = payload["props"]["attachments"][0]["actions"]
        # 2 valid choices + 1 "Other" = 3
        assert len(actions) == 3

    @pytest.mark.asyncio
    async def test_send_clarify_caps_at_19_choices(self):
        """No more than 19 choices (reserve 1 slot for 'Other')."""
        self.adapter._api_post = AsyncMock(return_value={"id": "clarify_post4"})
        many_choices = [f"Choice_{i}" for i in range(25)]
        result = await self.adapter.send_clarify(
            chat_id="chan_1",
            question="Pick:",
            choices=many_choices,
            clarify_id="clarify_4",
            session_key="agent:main:chan_1:123",
        )
        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        actions = payload["props"]["attachments"][0]["actions"]
        # max 19 choices + 1 "Other" = 20 actions
        assert len(actions) == 20


class TestMattermostSendUpdatePrompt(_InteractiveTestBase):
    @pytest.mark.asyncio
    async def test_send_update_prompt_sends_yes_no_buttons(self):
        """send_update_prompt sends Yes and No buttons."""
        self.adapter._api_post = AsyncMock(return_value={"id": "update_post"})

        result = await self.adapter.send_update_prompt(
            chat_id="chan_1",
            prompt="Apply update?",
            default="y",
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args[0][1]
        actions = payload["props"]["attachments"][0]["actions"]
        assert len(actions) == 2
        labels = [a["name"] for a in actions]
        assert any("Yes" in l or "✓" in l for l in labels)
        assert any("No" in l or "✗" in l for l in labels)

    @pytest.mark.asyncio
    async def test_send_update_prompt_fallback(self):
        """Without callback server, returns failure."""
        self.adapter._callback_runner = None
        result = await self.adapter.send_update_prompt(
            chat_id="chan_1", prompt="Update?", default="y",
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# Callback server — action handlers
# ---------------------------------------------------------------------------

class _CallbackTestBase:
    """Shared mocks for callback handler tests."""

    def setup_method(self):
        self.adapter = _make_adapter()
        # Mock _api_post and _api_get to prevent real HTTP calls
        self.adapter._api_post = AsyncMock(return_value={})
        self.adapter._api_get = AsyncMock(return_value={})
        self.adapter._bot_user_id = "bot_u_123"
        self.adapter._bot_username = "hermes-bot"


class TestMattermostActionCallbackDispatching(_CallbackTestBase):
    """Test that the action callback router dispatches to the right handler."""

    @pytest.mark.asyncio
    async def test_dispatch_exec_approval(self):
        """action_type 'exec_approval' routes to _handle_exec_approval_callback."""
        self.adapter._handle_exec_approval_callback = AsyncMock()
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "exec_approval", "approval_id": 1},
            "user_name": "alice",
        })
        await self.adapter._action_callback_handler(request)
        self.adapter._handle_exec_approval_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_slash_confirm(self):
        """action_type 'slash_confirm' routes to _handle_slash_confirm_callback."""
        self.adapter._handle_slash_confirm_callback = AsyncMock()
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "slash_confirm", "confirm_id": "c1"},
            "user_name": "alice",
        })
        await self.adapter._action_callback_handler(request)
        self.adapter._handle_slash_confirm_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_clarify(self):
        """action_type 'clarify' routes to _handle_clarify_callback."""
        self.adapter._handle_clarify_callback = AsyncMock()
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "clarify", "clarify_id": "cl1"},
            "user_name": "alice",
        })
        await self.adapter._action_callback_handler(request)
        self.adapter._handle_clarify_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_update_prompt(self):
        """action_type 'update_prompt' routes to _handle_update_prompt_callback."""
        self.adapter._handle_update_prompt_callback = AsyncMock()
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "update_prompt"},
            "user_name": "alice",
        })
        await self.adapter._action_callback_handler(request)
        self.adapter._handle_update_prompt_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_action_type_returns_400(self):
        """Unknown action_type returns a 400 response."""
        from aiohttp import web
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "unknown_type"},
            "user_name": "alice",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self):
        """Invalid JSON in request body returns 400."""
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("bad json"))
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 400


class TestMattermostBuildUpdateResponse(_CallbackTestBase):
    @pytest.mark.asyncio
    async def test_build_update_response(self):
        """_build_update_response returns JSON with update message."""
        resp = await self.adapter._build_update_response("alice", "✅ Approved")
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["update"]["message"] == "✅ Approved by @alice"
        assert body["update"]["attachments"] == []


class TestMattermostExecApprovalCallback(_CallbackTestBase):
    @pytest.mark.asyncio
    async def test_approved_once(self):
        """Clicking 'Allow Once' resolves approval with choice='once'."""
        self.adapter._approval_state[1] = "session_key_abc"
        mock_resolve = MagicMock(return_value=1)
        with patch("tools.approval.resolve_gateway_approval", mock_resolve):
            from aiohttp import web
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "exec_approval",
                    "approval_id": 1,
                    "choice": "once",
                },
                "user_name": "bob",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200
        mock_resolve.assert_called_once_with("session_key_abc", "once")

    @pytest.mark.asyncio
    async def test_approval_id_missing(self):
        """Missing approval_id returns 400."""
        from aiohttp import web
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "exec_approval"},
            "user_name": "bob",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_already_resolved_approval(self):
        """Double-clicking a resolved approval shows 'already resolved' message."""
        self.adapter._approval_state[1] = "session_key_abc"
        mock_resolve = MagicMock(return_value=1)
        with patch("tools.approval.resolve_gateway_approval", mock_resolve):
            from aiohttp import web
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "exec_approval",
                    "approval_id": 1,
                    "choice": "once",
                },
                "user_name": "bob",
            })
            # First click: resolves
            resp1 = await self.adapter._action_callback_handler(request)
            assert resp1.status == 200

            # Second click: state already popped → "already resolved"
            resp2 = await self.adapter._action_callback_handler(request)
            assert resp2.status == 200
            body2 = json.loads(resp2.body)
            assert "already been resolved" in body2["update"]["message"]

    @pytest.mark.asyncio
    async def test_deny_choice(self):
        """Deny click resolves with choice='deny'."""
        self.adapter._approval_state[2] = "session_key_xyz"
        mock_resolve = MagicMock(return_value=0)
        with patch("tools.approval.resolve_gateway_approval", mock_resolve):
            from aiohttp import web
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "exec_approval",
                    "approval_id": 2,
                    "choice": "deny",
                },
                "user_name": "bob",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200
        mock_resolve.assert_called_once_with("session_key_xyz", "deny")


class TestMattermostSlashConfirmCallback(_CallbackTestBase):
    @pytest.mark.asyncio
    async def test_approve_once(self):
        """Clicking 'Approve Once' resolves with choice='once'."""
        self.adapter._slash_confirm_state["cid_1"] = "session_key_1"
        mock_resolve = AsyncMock(return_value="")
        with patch("tools.slash_confirm.resolve", mock_resolve):
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "slash_confirm",
                    "confirm_id": "cid_1",
                    "choice": "once",
                },
                "user_name": "alice",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200
        mock_resolve.assert_called_once_with("session_key_1", "cid_1", "once")

    @pytest.mark.asyncio
    async def test_always_approve(self):
        """'Always Approve' resolves with choice='always'."""
        self.adapter._slash_confirm_state["cid_2"] = "session_key_2"
        mock_resolve = AsyncMock(return_value="saved")
        with patch("tools.slash_confirm.resolve", mock_resolve):
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "slash_confirm",
                    "confirm_id": "cid_2",
                    "choice": "always",
                },
                "user_name": "bob",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_confirm_id(self):
        """Missing confirm_id returns 400."""
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "slash_confirm"},
            "user_name": "alice",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_already_resolved_confirm(self):
        """Already-resolved confirm shows appropriate message."""
        self.adapter._slash_confirm_state["cid_resolved"] = "session_key"
        mock_resolve = AsyncMock(return_value="")
        with patch("tools.slash_confirm.resolve", mock_resolve):
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "slash_confirm",
                    "confirm_id": "cid_resolved",
                    "choice": "cancel",
                },
                "user_name": "alice",
            })
            # First click
            await self.adapter._action_callback_handler(request)
            # Second click — state already popped
            resp2 = await self.adapter._action_callback_handler(request)
            assert resp2.status == 200
            body2 = json.loads(resp2.body)
            assert "already been resolved" in body2["update"]["message"]


class TestMattermostClarifyCallback(_CallbackTestBase):
    @pytest.mark.asyncio
    async def test_choice_selected(self):
        """Clicking a choice button resolves with the choice text."""
        mock_resolve = MagicMock()
        with patch("tools.clarify_gateway.resolve_gateway_clarify", mock_resolve), \
             patch("tools.clarify_gateway.mark_awaiting_text") as _mock_mark:
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "clarify",
                    "clarify_id": "cl_1",
                    "response": "Option A",
                },
                "user_name": "alice",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200
        mock_resolve.assert_called_once_with("cl_1", "Option A")

    @pytest.mark.asyncio
    async def test_other_choice(self):
        """'Other' choice marks as awaiting text."""
        mock_mark = MagicMock()
        with patch("tools.clarify_gateway.mark_awaiting_text", mock_mark), \
             patch("tools.clarify_gateway.resolve_gateway_clarify") as _mock_resolve:
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "context": {
                    "action_type": "clarify",
                    "clarify_id": "cl_2",
                    "response": "__other__",
                },
                "user_name": "alice",
            })
            resp = await self.adapter._action_callback_handler(request)
            assert resp.status == 200
        mock_mark.assert_called_once_with("cl_2")
        _mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_clarify_id(self):
        """Missing clarify_id returns 400."""
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "clarify"},
            "user_name": "alice",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 400


class TestMattermostUpdatePromptCallback(_CallbackTestBase):
    @pytest.mark.asyncio
    async def test_yes_choice(self):
        """Yes button returns appropriate update message."""
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "update_prompt", "choice": "y"},
            "user_name": "alice",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "Yes" in body["update"]["message"]

    @pytest.mark.asyncio
    async def test_no_choice(self):
        """No button returns appropriate update message."""
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "context": {"action_type": "update_prompt", "choice": "n"},
            "user_name": "bob",
        })
        resp = await self.adapter._action_callback_handler(request)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "No" in body["update"]["message"]


# ---------------------------------------------------------------------------
# _resolve_root_id
# ---------------------------------------------------------------------------

class TestMattermostResolveRootId:
    def setup_method(self):
        self.adapter = _make_adapter()

    @pytest.mark.asyncio
    async def test_resolve_root_id_with_root_returns_root(self):
        """When the API returns a root_id, it should be returned."""
        self.adapter._api_get = AsyncMock(return_value={"root_id": "root_post_789"})
        result = await self.adapter._resolve_root_id("child_post_123")
        assert result == "root_post_789"

    @pytest.mark.asyncio
    async def test_resolve_root_id_without_root_returns_original(self):
        """When the post has no root_id, the original ID is returned."""
        self.adapter._api_get = AsyncMock(return_value={"id": "post_123", "root_id": ""})
        result = await self.adapter._resolve_root_id("post_123")
        assert result == "post_123"

    @pytest.mark.asyncio
    async def test_resolve_root_id_empty_input(self):
        """Empty post_id is returned as-is."""
        result = await self.adapter._resolve_root_id("")
        assert result == ""


# ---------------------------------------------------------------------------
# Slash command webhook handler (endpoint is on callback server)
# ---------------------------------------------------------------------------

class TestMattermostSlashRegister:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_123"
        self.adapter._slash_webhook_url = "http://mm-host:8580/mattermost/slash"

    @pytest.mark.asyncio
    async def test_register_skipped_when_url_not_set(self):
        """When _slash_webhook_url is empty, registration is skipped."""
        self.adapter._slash_webhook_url = ""
        await self.adapter._register_slash_commands()
        assert self.adapter._registered_slash_command_ids == []

    @pytest.mark.asyncio
    async def test_register_with_no_teams(self):
        """When teams API returns empty, no commands are registered."""
        self.adapter._api_get = AsyncMock(return_value=[])
        await self.adapter._register_slash_commands()
        assert self.adapter._registered_slash_command_ids == []

    @pytest.mark.asyncio
    async def test_register_registers_commands_per_team(self):
        """Commands are registered for each team the bot is in."""
        self.adapter._api_get = AsyncMock(return_value=[
            {"id": "team_a"},
            {"id": "team_b"},
        ])

        # The COMMAND_REGISTRY is real; use a small subset that is
        # gateway-available.  Mock _api_post to succeed for the first
        # command and return a valid ID.
        registered_ids = []
        async def mock_api_post(path, payload):
            nonlocal registered_ids
            if path == "commands":
                registered_ids.append(payload.get("trigger"))
                return {"id": f"cmd_{len(registered_ids)}"}
            return {}

        self.adapter._api_post = mock_api_post

        with patch("hermes_cli.commands._is_gateway_available", return_value=True), \
             patch("hermes_cli.commands._resolve_config_gates", return_value={}):
            await self.adapter._register_slash_commands()

        # Should have registered at least some commands
        assert len(self.adapter._registered_slash_command_ids) > 0
        # ids are stored for cleanup
        assert all(pid.startswith("cmd_") for pid in self.adapter._registered_slash_command_ids)

    @pytest.mark.asyncio
    async def test_register_api_failure_logged(self):
        """When API fails, it's logged and no crash."""
        self.adapter._api_get = AsyncMock(return_value=[{"id": "team_a"}])
        self.adapter._api_post = AsyncMock(return_value={})  # no "id" key -> failure

        with patch("hermes_cli.commands._is_gateway_available", return_value=True), \
             patch("hermes_cli.commands._resolve_config_gates", return_value={}):
            await self.adapter._register_slash_commands()

        # No commands registered due to API failure
        assert self.adapter._registered_slash_command_ids == []

    @pytest.mark.asyncio
    async def test_register_uses_slash_webhook_url(self):
        """webhook_url in payload is set from _slash_webhook_url, not _slash_webhook_public_url."""
        self.adapter._api_get = AsyncMock(return_value=[{"id": "team_a"}])
        captured_urls = []

        async def mock_api_post(path, payload):
            captured_urls.append(payload.get("url", ""))
            return {"id": "cmd_1"}

        self.adapter._api_post = mock_api_post

        with patch("hermes_cli.commands._is_gateway_available", return_value=True), \
             patch("hermes_cli.commands._resolve_config_gates", return_value={}):
            await self.adapter._register_slash_commands()

        assert len(captured_urls) > 0
        for url in captured_urls:
            assert url == "http://mm-host:8580/mattermost/slash"

    @pytest.mark.asyncio
    async def test_cleanup_deletes_registered_commands(self):
        """Cleanup calls DELETE for each registered command."""
        self.adapter._registered_slash_command_ids = ["cmd_1", "cmd_2", "cmd_3"]
        self.adapter._api_delete = AsyncMock(return_value=True)

        await self.adapter._cleanup_slash_commands()

        assert self.adapter._api_delete.call_count == 3
        self.adapter._api_delete.assert_any_call("commands/cmd_1")
        self.adapter._api_delete.assert_any_call("commands/cmd_2")
        self.adapter._api_delete.assert_any_call("commands/cmd_3")
        assert self.adapter._registered_slash_command_ids == []

    @pytest.mark.asyncio
    async def test_cleanup_empty_does_nothing(self):
        """Cleanup with no registered commands does nothing."""
        self.adapter._api_delete = AsyncMock()
        await self.adapter._cleanup_slash_commands()
        self.adapter._api_delete.assert_not_called()


class TestMattermostSlashWebhookHandler:
    def setup_method(self):
        self.adapter = _make_adapter()

    @pytest.mark.asyncio
    async def test_handle_slash_webhook_valid(self):
        """Valid slash command creates MessageEvent and calls handle_message."""
        self.adapter.handle_message = AsyncMock()

        request = MagicMock()
        request.json = AsyncMock(return_value={
            "channel_id": "ch_1",
            "user_id": "user_1",
            "user_name": "bob",
            "command": "/help",
            "text": "slash",
            "team_id": "team_a",
        })

        resp = await self.adapter._handle_slash_webhook(request)

        assert resp.status == 200
        body = json.loads(resp.body)
        assert "Processing" in body["text"]

        # _handle_slash_webhook uses asyncio.ensure_future to fire
        # handle_message in the background, so we need to let the
        # event loop run the scheduled task.
        await asyncio.sleep(0.01)

        # Verify handle_message was called with a COMMAND message event
        self.adapter.handle_message.assert_awaited_once()
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "/help slash"
        assert msg_event.message_type.value == "command"

    @pytest.mark.asyncio
    async def test_handle_slash_webhook_missing_fields(self):
        """Missing command or channel_id returns 400."""
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "channel_id": "ch_1",
            # no command
        })

        resp = await self.adapter._handle_slash_webhook(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_handle_slash_webhook_invalid_json(self):
        """Invalid JSON body returns 400."""
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("bad json"))

        resp = await self.adapter._handle_slash_webhook(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_slash_webhook_connect_flow(self):
        """connect() starts callback server and registers commands (no separate webhook)."""
        self.adapter._register_slash_commands = AsyncMock()
        self.adapter._start_callback_server = AsyncMock()
        self.adapter._api_get = AsyncMock(return_value={"id": "bot_1"})
        self.adapter._ws_loop = AsyncMock()
        self.adapter._session = AsyncMock()

        # Mock the aiohttp session creation
        import aiohttp
        with patch.object(aiohttp, "ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value = mock_session
            mock_session.close = AsyncMock()

            result = await self.adapter.connect()

        assert result is True
        self.adapter._register_slash_commands.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slash_webhook_disconnect_flow(self):
        """disconnect() cleans up slash commands and stops callback server (no separate webhook)."""
        self.adapter._cleanup_slash_commands = AsyncMock()
        self.adapter._stop_callback_server = AsyncMock()
        self.adapter._ws = MagicMock()
        self.adapter._ws.close = AsyncMock()
        self.adapter._session = AsyncMock()
        self.adapter._session.closed = False
        self.adapter._ws_task = None
        self.adapter._reconnect_task = None

        await self.adapter.disconnect()

        self.adapter._cleanup_slash_commands.assert_awaited_once()


# ---------------------------------------------------------------------------
# Reaction / queue acknowledgment
# ---------------------------------------------------------------------------

class TestMattermostReaction:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_123"

    @pytest.mark.asyncio
    async def test_add_reaction_calls_api(self):
        """_add_reaction POSTs to the reactions API."""
        self.adapter._api_post = AsyncMock(return_value={"id": "reaction_1"})

        result = await self.adapter._add_reaction("post_abc", "thumbsup")

        assert result is True
        self.adapter._api_post.assert_awaited_once_with(
            "posts/post_abc/reactions",
            {
                "user_id": "bot_123",
                "post_id": "post_abc",
                "emoji_name": "thumbsup",
            },
        )

    @pytest.mark.asyncio
    async def test_add_reaction_returns_false_on_failure(self):
        """When API returns empty, reaction is not added."""
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter._add_reaction("post_abc", "thumbsup")

        assert result is False

    @pytest.mark.asyncio
    async def test_add_reaction_no_post_id(self):
        """Empty post_id returns False without API call."""
        self.adapter._api_post = AsyncMock()

        result = await self.adapter._add_reaction("", "thumbsup")

        assert result is False
        self.adapter._api_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_reaction_strips_colons(self):
        """Emoji names with colons are normalized."""
        self.adapter._api_post = AsyncMock(return_value={"id": "r1"})

        await self.adapter._add_reaction("p1", ":hourglass_flowing_sand:")

        payload = self.adapter._api_post.call_args[0][1]
        assert payload["emoji_name"] == "hourglass_flowing_sand"

    @pytest.mark.asyncio
    async def test_on_message_queued_adds_reaction(self):
        """_on_message_queued adds hourglass reaction when message_id is set."""
        self.adapter._add_reaction = AsyncMock(return_value=True)

        event = MagicMock()
        event.message_id = "post_789"

        await self.adapter._on_message_queued(event)

        self.adapter._add_reaction.assert_awaited_once_with(
            "post_789", "hourglass_flowing_sand",
        )

    @pytest.mark.asyncio
    async def test_on_message_queued_skips_without_id(self):
        """_on_message_queued does nothing when message_id is empty."""
        self.adapter._add_reaction = AsyncMock()

        event = MagicMock()
        event.message_id = ""

        await self.adapter._on_message_queued(event)

        self.adapter._add_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_mattermost_top_level_channel_post_is_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "top_post_123",
        "user_id": "user_123",
        "channel_id": "chan_456",
        "message": "@hermes-bot start work",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "O",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id == "top_post_123"
    assert msg_event.source.message_id == "top_post_123"
    assert msg_event.message_id == "top_post_123"


@pytest.mark.asyncio
async def test_mattermost_dm_post_does_not_seed_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "dm_post_123",
        "user_id": "user_123",
        "channel_id": "dm_chan",
        "message": "hello",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "D",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id is None
    assert msg_event.source.message_id == "dm_post_123"
