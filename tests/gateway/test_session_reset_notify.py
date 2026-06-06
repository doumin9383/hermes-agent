"""Tests for session auto-reset notifications.

Verifies that:
- _should_reset() returns a reason string ("idle" or "daily") instead of bool
- SessionEntry captures auto_reset_reason
- SessionResetPolicy.notify controls whether notifications are sent
- notify_exclude_platforms skips notifications for excluded platforms
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
)
from gateway.session import SessionEntry, SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="123", user_id="u1"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
    )


def _make_store(policy=None, tmp_path=None):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    store = SessionStore(sessions_dir=tmp_path or "/tmp/test-sessions", config=config)
    return store


# ---------------------------------------------------------------------------
# _should_reset returns reason string
# ---------------------------------------------------------------------------

class TestShouldResetReason:
    def test_returns_none_when_not_expired(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="both", idle_minutes=60, at_hour=4),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),  # just updated
        )
        source = _make_source()
        assert store._should_reset(entry, source) is None

    def test_returns_idle_when_idle_expired(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),  # 60min ago > 30min threshold
        )
        source = _make_source()
        assert store._should_reset(entry, source) == "idle"

    def test_returns_daily_when_daily_boundary_crossed(self, tmp_path):
        now = datetime.now()
        store = _make_store(
            SessionResetPolicy(mode="daily", at_hour=now.hour),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=1),  # last active yesterday
        )
        source = _make_source()
        assert store._should_reset(entry, source) == "daily"

    def test_returns_none_when_mode_is_none(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="none"),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(days=30),
            updated_at=datetime.now() - timedelta(days=30),
        )
        source = _make_source()
        assert store._should_reset(entry, source) is None


# ---------------------------------------------------------------------------
# SessionEntry captures reason
# ---------------------------------------------------------------------------

class TestSessionEntryReason:
    def test_auto_reset_reason_stored(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        # Create initial session
        entry1 = store.get_or_create_session(source)
        assert not entry1.was_auto_reset

        # Age it past the idle threshold
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        # Next call should create a new session with reason
        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.auto_reset_reason == "idle"
        assert entry2.session_id != entry1.session_id

    def test_reset_had_activity_false_when_no_tokens(self, tmp_path):
        """Expired session with no tokens → reset_had_activity=False."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry1 = store.get_or_create_session(source)
        # No tokens used — session was idle with no conversation
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is False

    def test_reset_had_activity_true_when_tokens_used(self, tmp_path):
        """Expired session with tokens → reset_had_activity=True."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry1 = store.get_or_create_session(source)
        # Simulate some conversation happened
        entry1.total_tokens = 5000
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is True


# ---------------------------------------------------------------------------
# SessionResetPolicy notify config
# ---------------------------------------------------------------------------

class TestResetPolicyNotify:
    def test_notify_defaults_true(self):
        policy = SessionResetPolicy()
        assert policy.notify is True

    def test_notify_exclude_defaults(self):
        policy = SessionResetPolicy()
        assert "api_server" in policy.notify_exclude_platforms
        assert "webhook" in policy.notify_exclude_platforms

    def test_from_dict_with_notify_false(self):
        policy = SessionResetPolicy.from_dict({"notify": False})
        assert policy.notify is False

    def test_from_dict_with_custom_excludes(self):
        policy = SessionResetPolicy.from_dict({
            "notify_exclude_platforms": ["api_server", "webhook", "homeassistant"],
        })
        assert "homeassistant" in policy.notify_exclude_platforms

    def test_from_dict_preserves_defaults_on_missing_keys(self):
        policy = SessionResetPolicy.from_dict({})
        assert policy.notify is True
        assert "api_server" in policy.notify_exclude_platforms

    def test_to_dict_roundtrip(self):
        original = SessionResetPolicy(
            mode="idle",
            notify=False,
            notify_exclude_platforms=("api_server",),
        )
        restored = SessionResetPolicy.from_dict(original.to_dict())
        assert restored.notify == original.notify
        assert restored.notify_exclude_platforms == original.notify_exclude_platforms
        assert restored.mode == original.mode


# ---------------------------------------------------------------------------
# SessionEntry to_dict / from_dict roundtrip for auto-reset fields
# ---------------------------------------------------------------------------

class TestSessionEntryAutoResetRoundtrip:
    def test_was_auto_reset_persists_across_roundtrip(self, tmp_path):
        """was_auto_reset=True survives to_dict() → from_dict() (gateway restart)."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.auto_reset_reason == "idle"
        assert entry2.session_id != entry.session_id

        # Simulate gateway restart: reload from disk
        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry2.session_key)
        assert reloaded is not None
        assert reloaded.was_auto_reset is True
        assert reloaded.auto_reset_reason == "idle"

    def test_reset_had_activity_persists_across_roundtrip(self, tmp_path):
        """reset_had_activity survives to_dict() → from_dict() (gateway restart)."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        entry.total_tokens = 1000
        entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.reset_had_activity is True

        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry2.session_key)
        assert reloaded is not None
        assert reloaded.reset_had_activity is True

    def test_auto_reset_reason_none_roundtrip(self, tmp_path):
        """auto_reset_reason=None (no reset) survives roundtrip cleanly."""
        store = _make_store(tmp_path=tmp_path)
        source = _make_source()

        entry = store.get_or_create_session(source)
        assert entry.was_auto_reset is False

        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry.session_key)
        assert reloaded is not None
        assert reloaded.was_auto_reset is False
        assert reloaded.auto_reset_reason is None
        assert reloaded.reset_had_activity is False


# ---------------------------------------------------------------------------
# Regression: _handle_message_with_agent passes include_ancestors=True
# to load_transcript when session was auto-reset
# ---------------------------------------------------------------------------


class TestAutoResetLoadTranscriptArgs:
    """Verifies that _handle_message_with_agent passes
    include_ancestors=True / max_ancestor_messages=50 to load_transcript
    when session_entry.was_auto_reset is True.

    Before the fix (bug):
        was_auto_reset was cleared to False BEFORE the history-loading block
        re-read it via ``getattr(session_entry, 'was_auto_reset', False)``,
        so ``include_ancestors`` was always False for auto-reset sessions.

    After the fix:
        The flag is saved to a local variable (*_was_auto_reset*) before
        being cleared on the SessionEntry, and the local is used instead
        of re-reading from the entry.
    """

    @pytest.mark.asyncio
    async def test_load_transcript_called_with_include_ancestors_for_auto_reset(
        self, tmp_path, monkeypatch,
    ):
        from gateway.run import GatewayRunner
        from gateway.session import SessionStore, SessionEntry, SessionSource
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway.platforms.base import MessageEvent, MessageType
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        # ── Setup real SessionStore ──────────────────────────────────
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="test-auto-reset",
            user_id="u1",
        )
        session_key = store._generate_session_key(source)

        # Insert an entry that looks like an auto-reset result (same
        # fields that get_or_create_session sets after an idle/daily
        # expiry).  This is the session state *before*
        # _handle_message_with_agent runs.
        entry = SessionEntry(
            session_key=session_key,
            session_id="auto-reset-session-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            was_auto_reset=True,
            auto_reset_reason="idle",
            reset_had_activity=True,
        )
        store._entries[session_key] = entry

        # ── Setup minimal GatewayRunner ──────────────────────────────
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
        )

        # Adapter needed for auto-reset notification
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}

        runner.session_store = store

        # Dicts/sets used throughout _handle_message_with_agent
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._session_run_generation = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._session_model_overrides = {}
        runner._pending_model_notes = {}
        runner._voice_mode = {}
        runner._background_tasks = set()
        runner._draining = False
        runner._restart_requested = False
        runner._restart_task_started = False
        runner._restart_detached = False
        runner._restart_via_service = False
        runner._restart_drain_timeout = 0.0
        runner._stop_task = None
        runner._exit_code = None

        runner._update_runtime_status = MagicMock()
        runner._is_user_authorized = lambda _source: True
        runner.hooks = MagicMock()
        runner.hooks.emit = AsyncMock()
        runner.delivery_router = MagicMock()

        # Extra attributes accessed during _handle_message_with_agent
        runner._session_sources = {}
        runner._prefill_messages = []
        runner._session_db = None
        runner._telegram_topic_bindings = {}
        runner._pending_native_image_paths_by_session = {}

        # ── Patch load_transcript with a spy ─────────────────────────
        mock_load = MagicMock(return_value=[])
        store.load_transcript = mock_load  # type: ignore[method-assign]

        # ── Patch _run_agent to avoid actual AI execution ────────────
        monkeypatch.setattr(
            runner, "_run_agent",
            AsyncMock(return_value={"final_response": "ok", "response": "hello"}),
        )

        # ── Create event ─────────────────────────────────────────────
        event = MessageEvent(
            text="hello after auto-reset",
            message_type=MessageType.TEXT,
            source=source,
        )

        # ── Call the handler ─────────────────────────────────────────
        try:
            result = await runner._handle_message_with_agent(
                event, source, "test-key", 1,
            )
        except Exception as exc:
            pytest.fail(
                f"_handle_message_with_agent raised: {exc}\n"
                f"Check missing runner attributes or un-mocked dependencies.",
            )

        # ── Assertions ───────────────────────────────────────────────
        # load_transcript must have been called with include_ancestors=True.
        # Since the mock replaces the real load_transcript entirely, there
        # is exactly one call — the one in _handle_message_with_agent.
        calls = mock_load.call_args_list
        assert len(calls) >= 1, "load_transcript was never called"
        ancestor_calls = [
            c for c in calls
            if c[1].get("include_ancestors") is True
        ]
        assert len(ancestor_calls) >= 1, (
            f"load_transcript was never called with include_ancestors=True. "
            f"All calls: {calls}"
        )
        assert ancestor_calls[0][1].get("max_ancestor_messages") == 50, (
            f"Expected max_ancestor_messages=50, "
            f"got {ancestor_calls[0][1].get('max_ancestor_messages')}"
        )

        # The SessionEntry flags must have been cleared (one-shot contract)
        assert entry.was_auto_reset is False, (
            "was_auto_reset must be False after consumption"
        )
        assert entry.auto_reset_reason is None, (
            "auto_reset_reason must be None after consumption"
        )
