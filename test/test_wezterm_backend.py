from __future__ import annotations

import pytest

import terminal


def test_wezterm_pane_shares_window_uses_exact_tab(monkeypatch) -> None:
    panes = [
        {"pane_id": 1, "window_id": 10, "tab_id": 20},
        {"pane_id": 2, "window_id": 10, "tab_id": 20},
        {"pane_id": 3, "window_id": 10, "tab_id": 21},
    ]
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(backend, "_list_panes", lambda: panes)

    assert backend.pane_shares_window("1", "2") is True
    assert backend.pane_shares_window("1", "3") is False
    assert backend.pane_shares_window("1", "99") is False


def test_wezterm_strict_cwd_check_does_not_fail_open(monkeypatch) -> None:
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(backend, "_list_panes", lambda: [{"pane_id": 1, "cwd": ""}])
    assert backend.pane_matches_cwd_strict("1", "/home/musta/dev/project") is False

    monkeypatch.setattr(
        backend,
        "_list_panes",
        lambda: [{"pane_id": 1, "cwd": "file:///home/musta/dev/project"}],
    )
    assert backend.pane_matches_cwd_strict("1", "/home/musta/dev/project") is True


def test_wezterm_pane_liveness_unknown_when_listing_fails(monkeypatch) -> None:
    # `_list_panes()` returns None whenever `wezterm cli list` errors or
    # times out -- this must surface as "unknown", never as "gone" (that
    # conflation is the exact bug `pane_liveness` exists to fix; `is_alive`
    # keeps its own, unchanged, folding of this case into False).
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(backend, "_list_panes", lambda timeout=1.0: None)
    assert backend.pane_liveness("1") == "unknown"
    assert backend.is_alive("1") is False


def test_wezterm_pane_liveness_gone_only_on_successful_listing_without_pane(monkeypatch) -> None:
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(backend, "_list_panes", lambda timeout=1.0: [])
    assert backend.pane_liveness("1") == "gone"

    monkeypatch.setattr(backend, "_list_panes", lambda timeout=1.0: [{"pane_id": 2, "title": "other"}])
    assert backend.pane_liveness("1") == "gone"


def test_wezterm_pane_liveness_alive_by_id_or_title_marker(monkeypatch) -> None:
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(backend, "_list_panes", lambda timeout=1.0: [{"pane_id": 1, "title": "other"}])
    assert backend.pane_liveness("1") == "alive"

    monkeypatch.setattr(
        backend, "_list_panes", lambda timeout=1.0: [{"pane_id": 2, "title": "CCB-Claude-abc"}]
    )
    assert backend.pane_liveness("CCB-Claude-abc") == "alive"


def test_wezterm_send_text_raises_when_enter_cannot_be_sent(monkeypatch) -> None:
    backend = terminal.WeztermBackend()
    monkeypatch.setattr(terminal, "_run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(backend, "_send_enter", lambda _pane_id: False)

    with pytest.raises(RuntimeError, match="failed to submit text"):
        backend.send_text("1", "hello")


def test_wezterm_send_enter_reports_exhausted_retries(monkeypatch) -> None:
    backend = terminal.WeztermBackend()
    monkeypatch.setenv("CCB_WEZTERM_ENTER_DELAY", "0")
    monkeypatch.setattr(terminal.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(backend, "_send_key_cli", lambda *_args: False)
    monkeypatch.setattr(
        terminal,
        "_run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )

    assert backend._send_enter("1") is False
