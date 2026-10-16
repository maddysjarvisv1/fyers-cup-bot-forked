"""Tests for run.py stdout line-buffering and cycle-summary flush behavior."""
import ast
import io
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import run


def test_redirected_file_stdout_flushes_immediately_in_process(tmp_path, monkeypatch):
    """Redirecting stdout to a block-buffered file and configuring stdout via
    run.py makes bare print() appear on disk immediately without closing or
    exiting the process."""
    log_file = tmp_path / "fyers_run.log"
    # Open explicitly block-buffered (simulating non-tty file redirection)
    fh = open(log_file, "w", encoding="utf-8", buffering=8192)
    try:
        monkeypatch.setattr(sys, "stdout", fh)
        assert not fh.line_buffering

        run._configure_stdout()
        assert fh.line_buffering

        print("cycle ok | quotes 12 | open orders 1 | positions 0")
        # Read back immediately from disk while process & file handle stay open
        on_disk = log_file.read_text(encoding="utf-8")
        assert "cycle ok | quotes 12 | open orders 1 | positions 0\n" in on_disk
    finally:
        fh.close()


def test_main_configures_line_buffering_and_logs_immediately(tmp_path, monkeypatch):
    """run.main() reconfigures a redirected file stdout at startup so bare
    print() lines hit the file immediately before process exit."""
    log_file = tmp_path / "main_redirected.log"
    fh = open(log_file, "w", encoding="utf-8", buffering=8192)
    observed_during_run = {}
    try:
        monkeypatch.setattr(sys, "stdout", fh)
        monkeypatch.setattr(run, "_acquire_single_instance", lambda *a, **kw: None)
        monkeypatch.setattr(run, "load_env", lambda: {"MODE": "live", "CONFIRM_LIVE": "NO"})
        monkeypatch.delenv("MODE", raising=False)
        monkeypatch.delenv("CONFIRM_LIVE", raising=False)

        run.main()
        # Still inside the process with fh open and not manually flushed:
        print("post-main bare print check")
        observed_during_run["content"] = log_file.read_text(encoding="utf-8")
    finally:
        fh.close()

    assert "MODE=live but CONFIRM_LIVE is not YES" in observed_during_run["content"]
    assert "post-main bare print check\n" in observed_during_run["content"]


def test_guard_degrades_safely_without_reconfigure(monkeypatch):
    """When sys.stdout lacks .reconfigure (AttributeError), startup does not
    crash and still logs normally."""
    class LegacyStdout:
        def __init__(self):
            self.buf = io.StringIO()

        def write(self, s):
            return self.buf.write(s)

        def flush(self):
            self.buf.flush()

    legacy = LegacyStdout()
    assert not hasattr(legacy, "reconfigure")
    monkeypatch.setattr(sys, "stdout", legacy)
    monkeypatch.setattr(run, "_acquire_single_instance", lambda *a, **kw: None)
    monkeypatch.setattr(run, "load_env", lambda: {"MODE": "live", "CONFIRM_LIVE": "NO"})
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("CONFIRM_LIVE", raising=False)

    # Must not raise AttributeError; must still start and log
    run.main()
    assert "MODE=live but CONFIRM_LIVE is not YES" in legacy.buf.getvalue()


def test_cycle_summary_print_has_explicit_flush():
    """The cycle-summary print in run.py has flush=True so the operator-watched
    heartbeat line never lags even on streams without reconfigure."""
    src = pathlib.Path(run.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            call_src = ast.get_source_segment(src, node) or ""
            if "cycle ok" in call_src:
                found = True
                flush_kw = [kw for kw in node.keywords if kw.arg == "flush"]
                assert flush_kw and isinstance(flush_kw[0].value, ast.Constant) and flush_kw[0].value.value is True
    assert found, "cycle ok print call not found in run.py"
