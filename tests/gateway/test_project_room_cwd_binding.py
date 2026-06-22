import os
from pathlib import Path

from agent.runtime_cwd import resolve_agent_cwd
from gateway.config import Platform
from gateway.session import SessionContext, SessionSource
from gateway.session_context import clear_session_vars
from gateway.project_binding import (
    _PROJECT_BINDING_CACHE,
    resolve_project_binding_for_chat,
    resolve_project_cwd_for_chat,
)


def _reset_project_binding_cache() -> None:
    _PROJECT_BINDING_CACHE.update({"path": None, "mtime_ns": None, "data": None})


def test_resolve_project_binding_for_chat_parses_configmap_wrapper(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg = tmp_path / "projects.yaml"
    cfg.write_text(
        """
apiVersion: v1
kind: ConfigMap
data:
  projects.yaml: |
    projects:
      hermes-agent-fork:
        room: "room-123"
        defaultCwd: %s
        path: %s
"""
        % (repo_dir, repo_dir),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROJECT_CONFIG_PATH", str(cfg))
    _reset_project_binding_cache()

    binding = resolve_project_binding_for_chat("room-123")

    assert binding["project"] == "hermes-agent-fork"
    assert binding["defaultCwd"] == str(repo_dir)



def test_resolve_project_cwd_for_chat_requires_existing_directory(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    missing_dir = tmp_path / "missing"
    cfg = tmp_path / "projects.yaml"
    cfg.write_text(
        f"""
projects:
  ok:
    room: room-ok
    defaultCwd: {repo_dir}
  missing:
    room: room-missing
    defaultCwd: {missing_dir}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROJECT_CONFIG_PATH", str(cfg))
    _reset_project_binding_cache()

    assert resolve_project_cwd_for_chat("room-ok") == str(repo_dir)
    assert resolve_project_cwd_for_chat("room-missing") == ""
    assert resolve_project_cwd_for_chat("room-unknown") == ""



def test_set_session_env_pins_runtime_cwd_from_room_binding(tmp_path, monkeypatch):
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg = tmp_path / "projects.yaml"
    cfg.write_text(
        f"""
projects:
  bound-project:
    room: room-ctx
    defaultCwd: {repo_dir}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROJECT_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("TERMINAL_CWD", str(fallback_dir))
    _reset_project_binding_cache()

    runner = GatewayRunner.__new__(GatewayRunner)
    context = SessionContext(
        source=SessionSource(platform=Platform.MATTERMOST, chat_id="room-ctx", message_id="msg-1"),
        connected_platforms=[],
        home_channels={},
        session_key="sess-1",
    )

    tokens = runner._set_session_env(context)
    try:
        assert resolve_agent_cwd() == Path(repo_dir)
    finally:
        clear_session_vars(tokens)

    assert resolve_agent_cwd() == Path(fallback_dir)
