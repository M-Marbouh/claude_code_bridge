"""F1: the suite's per-test wall-clock guard.

The guard exists because one adapter test once spun for two hours: a fake clock
advanced only inside a callback the hung loop never reached, with `time.sleep`
patched out. Nothing bounded a single test, so the run stalled and the work
queued behind it died.

These tests prove the guard END TO END by running a deliberately looping test in
a SUBPROCESS. Doing it in-process would be self-defeating (the alarm would fire
inside the very test that is asserting it fires), and an outer `timeout` proves
only that the suite is slow, not that the guard reported the failure.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEST_DIR = REPO / "test"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="signal.alarm is POSIX only")


def _child_env(timeout_env: str | None = "3") -> dict:
    """The environment a probe subprocess needs.

    `pytest` may live in a user site-packages the child would not otherwise
    search (the suite is often run via an activated install), so the exact
    locations this process imported it from are propagated.
    """
    env = dict(os.environ)
    env.pop("CCB_TEST_TIMEOUT_S", None)
    if timeout_env is not None:
        env["CCB_TEST_TIMEOUT_S"] = timeout_env
    import pytest as _pytest

    roots = {
        str(Path(_pytest.__file__).resolve().parent.parent),
        str(Path(__file__).resolve().parents[1] / "lib"),
    }
    existing = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    env["PYTHONPATH"] = os.pathsep.join([*sorted(roots), *existing])
    return env


def _run_probe(tmp_path: Path, source: str, *, timeout_env: str | None = "3") -> subprocess.CompletedProcess:
    """Run one probe test file under THIS repo's conftest, in a subprocess."""
    probe = TEST_DIR / f"_guard_probe_{tmp_path.name}.py"
    probe.write_text(textwrap.dedent(source), encoding="utf-8")
    env = _child_env(timeout_env)
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def test_a_looping_test_is_killed_and_reported(tmp_path):
    """The guard's whole purpose: a spinning test fails FAST, not in two hours."""
    result = _run_probe(
        tmp_path,
        """
        import time

        def test_spins_forever():
            time.sleep(60)
        """,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "exceeded its 3s limit" in combined
    # It failed by the GUARD, not by the outer watchdog or a plain sleep.
    assert "long_test" in combined


def test_a_fast_test_still_passes_under_the_guard(tmp_path):
    result = _run_probe(
        tmp_path,
        """
        def test_trivial():
            assert 1 + 1 == 2
        """,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_the_marker_opts_a_long_test_out(tmp_path):
    """A test that legitimately needs longer can say so."""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.long_test
        def test_needs_four_seconds():
            time.sleep(4)
            assert True
        """,
        timeout_env="2",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("value", ["", "   ", "not-a-number", "nan", "inf", "-1", "0"])
def test_a_bad_override_falls_back_to_the_default(value):
    """A guard you can disable by typo is worse than no guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ccb_conftest_guard", str(TEST_DIR / "conftest.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    previous = os.environ.get("CCB_TEST_TIMEOUT_S")
    try:
        os.environ["CCB_TEST_TIMEOUT_S"] = value
        limit = module.test_timeout_s()
    finally:
        if previous is None:
            os.environ.pop("CCB_TEST_TIMEOUT_S", None)
        else:
            os.environ["CCB_TEST_TIMEOUT_S"] = previous

    # "0" is the ONE documented way to turn the guard off; everything else that
    # cannot be parsed as a positive number keeps the default.
    if value == "0":
        assert limit is None
    else:
        assert limit == module.DEFAULT_TEST_TIMEOUT_S


def test_a_positive_override_is_honoured():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ccb_conftest_guard2", str(TEST_DIR / "conftest.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    previous = os.environ.get("CCB_TEST_TIMEOUT_S")
    try:
        os.environ["CCB_TEST_TIMEOUT_S"] = "7.5"
        assert module.test_timeout_s() == 7.5
    finally:
        if previous is None:
            os.environ.pop("CCB_TEST_TIMEOUT_S", None)
        else:
            os.environ["CCB_TEST_TIMEOUT_S"] = previous


def test_the_guard_survives_an_except_exception_loop(tmp_path):
    """ITEM 4: the expiry must not be swollowed by `except Exception`.

    This is the exact shape that defeated the first guard: a `while True` with a
    broad handler around it. A `TimeoutError` is an `Exception`, so that loop
    caught it and kept going -- the test then ran forever with the guard
    "active".
    """
    result = _run_probe(
        tmp_path,
        """
        import time

        def test_swallows_everything():
            while True:
                try:
                    time.sleep(30)
                except Exception:
                    # A one-shot Exception-based guard dies right here.
                    continue
        """,
        timeout_env="3",
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "exceeded its 3s limit" in combined


def test_the_long_test_marker_registers_under_strict_markers(tmp_path):
    """ITEM 4: `--strict-markers` must accept @pytest.mark.long_test.

    The marker is registered in `pytest_configure(config)`. When that signature
    was `config=None`, pytest still called it with a config, the registration
    branch was skipped, and `--strict-markers` failed COLLECTION.
    """
    probe = TEST_DIR / f"_guard_marker_{tmp_path.name}.py"
    probe.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.long_test
            def test_marked():
                assert True
            """
        ),
        encoding="utf-8",
    )
    env = _child_env(None)
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", str(probe),
                "--strict-markers", "-q", "-p", "no:cacheprovider",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()

    assert "not registered" not in (result.stdout + result.stderr), (
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
