from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

from ccb_cleanup import DEFAULT_TTL_SECONDS, PrunePlan, PruneResult, plan_prune, run_prune


ROOT = Path(__file__).resolve().parents[1]
NOW = 2_000_000_000


def _load_ccb_module() -> object:
    ccb_path = ROOT / "ccb"
    loader = SourceFileLoader("ccb_clean_script", str(ccb_path))
    spec = importlib.util.spec_from_loader("ccb_clean_script", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _record_path(run_dir: Path, session_id: str) -> Path:
    return run_dir / f"ccb-session-ai-{session_id}.json"


def _write_record(
    run_dir: Path,
    session_id: str,
    *,
    project_id: str,
    updated_at: int,
    providers: dict[str, dict] | None = None,
    ccb_session_id: str | None = None,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = _record_path(run_dir, session_id)
    path.write_text(
        json.dumps(
            {
                "ccb_session_id": ccb_session_id or f"ai-{session_id}",
                "ccb_project_id": project_id,
                "work_dir": f"/tmp/{project_id}",
                "terminal": "tmux",
                "updated_at": updated_at,
                "providers": providers or {"codex": {"pane_id": "%1", "pane_title_marker": f"CCB-Codex-{session_id}"}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _dead_liveness(_record: dict, _provider: str) -> bool:
    return False


def test_plan_prune_respects_keep_newest_window(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    for index in range(7):
        _write_record(run_dir, str(index), project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 100 + index)

    plan = plan_prune(run_dir=run_dir, keep=5, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=_dead_liveness)

    assert plan.scanned == 7
    assert len(plan.prunable) == 2
    assert {Path(record.path).name for record in plan.prunable} == {
        "ccb-session-ai-0.json",
        "ccb-session-ai-1.json",
    }
    assert plan.summary["skipped_keep_newest"] == 5


def test_plan_prune_only_stale_dead_records_are_prunable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stale = _write_record(run_dir, "stale", project_id="p1", updated_at=NOW - 1_000)
    fresh = _write_record(run_dir, "fresh", project_id="p1", updated_at=NOW - 10)

    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=100, now=NOW, liveness_func=_dead_liveness)

    assert [Path(record.path) for record in plan.prunable] == [stale]
    assert fresh in {Path(record.path) for record in plan.kept}
    assert plan.summary["skipped_not_old_enough"] == 1


def test_plan_prune_never_prunes_live_record(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    live = _write_record(run_dir, "live", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)

    def liveness(_record: dict, provider: str) -> bool:
        assert provider == "codex"
        return True

    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=liveness)

    assert plan.prunable == []
    assert Path(plan.kept[0].path) == live
    assert plan.kept[0].reason == "live"
    assert plan.summary["skipped_live"] == 1


def test_plan_prune_never_prunes_running_ccb_record(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    running = _write_record(
        run_dir,
        "running",
        project_id="p1",
        updated_at=NOW - DEFAULT_TTL_SECONDS - 1,
        ccb_session_id="ai-1999990000-4242",
    )

    plan = plan_prune(
        run_dir=run_dir,
        keep=0,
        older_than_seconds=DEFAULT_TTL_SECONDS,
        now=NOW,
        liveness_func=_dead_liveness,
        process_alive_func=lambda pid: pid == 4242,
    )

    assert plan.prunable == []
    assert Path(plan.kept[0].path) == running
    assert plan.kept[0].reason == "running_ccb"
    assert plan.summary["skipped_running"] == 1


def test_plan_prune_keeps_unknown_liveness(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_record(run_dir, "unknown", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)

    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=lambda *_: None)

    assert plan.prunable == []
    assert plan.kept[0].reason == "liveness_unknown"
    assert plan.summary["skipped_unknown"] == 1


def test_run_prune_dry_run_deletes_nothing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    target = _write_record(run_dir, "old", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)
    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=_dead_liveness)

    result = run_prune(plan, dry_run=True)

    assert result.dry_run is True
    assert result.planned == 1
    assert result.deleted == 0
    assert target.exists()


def test_run_prune_deletes_only_planned_stale_dead_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    dead = _write_record(run_dir, "dead", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)
    live = _write_record(run_dir, "live", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 2)

    def liveness(record: dict, _provider: str) -> bool:
        return str(record.get("ccb_session_id")) == "ai-live"

    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=liveness)
    result = run_prune(plan)

    assert result.deleted == 1
    assert result.failed == 0
    assert not dead.exists()
    assert live.exists()


def test_plan_prune_honors_keep_and_older_than(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_record(run_dir, "oldest", project_id="p1", updated_at=NOW - 500)
    _write_record(run_dir, "middle", project_id="p1", updated_at=NOW - 300)
    _write_record(run_dir, "newest", project_id="p1", updated_at=NOW - 200)

    plan = plan_prune(run_dir=run_dir, keep=1, older_than_seconds=250, now=NOW, liveness_func=_dead_liveness)

    assert {Path(record.path).name for record in plan.prunable} == {
        "ccb-session-ai-oldest.json",
        "ccb-session-ai-middle.json",
    }
    assert plan.summary["skipped_keep_newest"] == 1


def test_plan_prune_scope_single_project_vs_all_projects(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    p1 = _write_record(run_dir, "p1", project_id="p1", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)
    p2 = _write_record(run_dir, "p2", project_id="p2", updated_at=NOW - DEFAULT_TTL_SECONDS - 1)

    single = plan_prune(
        run_dir=run_dir,
        project="p1",
        keep=0,
        older_than_seconds=DEFAULT_TTL_SECONDS,
        now=NOW,
        liveness_func=_dead_liveness,
    )
    all_projects = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=DEFAULT_TTL_SECONDS, now=NOW, liveness_func=_dead_liveness)

    assert [Path(record.path) for record in single.prunable] == [p1]
    assert {Path(record.path) for record in all_projects.prunable} == {p1, p2}


def test_plan_summary_counts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_record(run_dir, "dead", project_id="p1", updated_at=NOW - 500)
    _write_record(run_dir, "fresh", project_id="p1", updated_at=NOW - 10)
    _write_record(run_dir, "live", project_id="p2", updated_at=NOW - 500)
    _write_record(run_dir, "unknown", project_id="p3", updated_at=NOW - 500)

    def liveness(record: dict, _provider: str) -> bool | None:
        sid = str(record.get("ccb_session_id") or "")
        if sid == "ai-live":
            return True
        if sid == "ai-unknown":
            return None
        return False

    plan = plan_prune(run_dir=run_dir, keep=0, older_than_seconds=100, now=NOW, liveness_func=liveness)

    assert plan.summary["scanned"] == 4
    assert plan.summary["prunable"] == 1
    assert plan.summary["kept"] == 3
    assert plan.summary["skipped_not_old_enough"] == 1
    assert plan.summary["skipped_live"] == 1
    assert plan.summary["skipped_unknown"] == 1


def test_cmd_clean_forwards_flags_to_prune_engine(monkeypatch, tmp_path: Path, capsys) -> None:
    ccb = _load_ccb_module()
    calls: list[dict] = []

    def fake_plan_prune(**kwargs):
        calls.append(kwargs)
        return PrunePlan(
            keep=kwargs["keep"],
            older_than_seconds=kwargs["older_than_seconds"],
            project=None,
            run_dir=str(tmp_path),
            scanned=0,
            kept=[],
            prunable=[],
            summary={"scanned": 0, "kept": 0, "prunable": 0},
        )

    def fake_run_prune(plan, *, dry_run: bool = False):
        return PruneResult(
            dry_run=dry_run,
            scanned=0,
            planned=0,
            kept=0,
            deleted=0,
            failed=0,
            missing=0,
            failures=[],
            plan=plan,
        )

    monkeypatch.setattr(ccb, "plan_prune", fake_plan_prune)
    monkeypatch.setattr(ccb, "run_prune", fake_run_prune)
    artifact_calls: list[dict] = []
    monkeypatch.setattr(
        ccb,
        "cleanup_reply_artifacts",
        lambda **kwargs: artifact_calls.append(kwargs) or 2,
    )
    monkeypatch.setattr(sys, "argv", ["ccb", "clean", "--all-projects", "--dry-run", "--keep", "2", "--older-than", "12h", "--json"])

    assert ccb.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert calls == [{"keep": 2, "older_than_seconds": 43_200, "project": None}]
    assert artifact_calls == [
        {
            "project": None,
            "all_projects": True,
            "older_than_seconds": 43_200,
            "dry_run": True,
        }
    ]
    assert out["dry_run"] is True
    assert out["reply_artifacts"]["planned"] == 2


def test_auto_prune_disabled_by_env(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setenv("CCB_NO_AUTO_PRUNE", "1")
    launcher = ccb.AILauncher(providers=["codex"])
    calls: list[str] = []
    monkeypatch.setattr(ccb, "plan_prune", lambda **_kwargs: calls.append("plan"))

    launcher._auto_prune_session_records()

    assert calls == []
