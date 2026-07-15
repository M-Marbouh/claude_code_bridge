from __future__ import annotations

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
