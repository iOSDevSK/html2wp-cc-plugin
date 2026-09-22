#!/usr/bin/env python3
"""progress.sh summary reports each stage's own clock from its start/done
marks, so a run with no timing hook (a worktree, a plain skill copy, Codex)
still says where the time went.

  python3 test-progress-summary.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [
    {"t": 1000.0, "event": "start", "stage": "-1"},
    {"t": 1600.0, "event": "done", "stage": "-1"},            # 10:00
    {"t": 1600.5, "event": "start", "stage": "2"},
    {"t": 1650.5, "event": "fail", "stage": "2", "note": "gate A"},
    {"t": 1700.5, "event": "start", "stage": "2"},
    {"t": 1790.5, "event": "done", "stage": "2"},              # 0:50 + 1:30 = 2:20
    {"t": 1790.6, "event": "done", "stage": "2.5"},            # no start: no clock
    {"t": 1800.0, "event": "script", "stage": "2", "script": "stage2-gates.sh", "ms": 80000, "ok": True},
]
with tempfile.TemporaryDirectory() as t:
    (Path(t) / ".h2wp-timing.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = subprocess.run(["bash", str(HERE / "progress.sh"), "summary", t], capture_output=True, text=True).stdout
lines = {l.split()[0]: l for l in out.splitlines() if l.strip() and l.split()[0] in ("-1", "2", "2.5")}
checks = [
    ("-1" in lines and "10:00" in lines["-1"], "stage -1 clock 10:00"),
    ("2" in lines and "2:20" in lines["2"] and "1:20" in lines["2"], "stage 2 clock 2:20 across a fail and a rerun, scripts 1:20"),
    ("2.5" in lines and "—" in lines["2.5"], "a stage with no start has no clock"),
]
failed = [name for ok, name in checks if not ok]
for ok, name in checks:
    print(("ok   " if ok else "FAIL ") + name)
if failed:
    print(out)
sys.exit(1 if failed else 0)
