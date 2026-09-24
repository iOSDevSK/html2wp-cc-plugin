#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""
The progress hook runs after EVERY Bash call in every session with the plugin
loaded, so the two ways it can be wrong are both expensive: a row or a
reminder for a command that ran nothing (noise, and a boundary announced that
was never reached), or a crash (a hook error on every single call).

The first real session found both kinds of false positive this file pins down:
`grep … allowance.sh` logged as a run, and a Python test table inside a
heredoc — whose string literals held `&& node … verify-parity.mjs` —
announcing the stage 2 boundary.

    python3 skill/hooks/test-stage-progress.py        exit 0 = all pass
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("stage_progress", os.path.join(HERE, "stage-progress.py"))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n      got:  {got!r}\n      want: {want!r}")


# ---------------------------------------------------------------- run vs mention
RUNS = [
    # reading, searching, editing a script is not running it
    ("cd x && grep -n -E 'curl|key' allowance.sh | head -15", []),
    ("grep -n -i 'shasum' SKILL.md assets/scripts/whats-here.sh | head", []),
    ("sed -n 1,40p verify-wp.py; cat convert-remote.sh", []),
    ("vim prerender-spa.py", []),
    ("echo 'python3 verify-wp.py' # just a string", []),
    ('echo "a && python3 x/verify-wp.py --wp y"', []),
    # parsing it is not running it either
    ("bash -n progress.sh && python3 -m py_compile verify-wp.py && node --check chrome-groups.mjs", []),
    ("python3 -c 'import json; print(1)' verify-wp.py", []),
    ("for f in $(find s -name '*.sh'); do bash -n \"$f\"; done", []),
    # a heredoc body is data, whatever it says
    ("python3 - <<'EOF'\ncases = [(\"python3 a/verify-static.py && node a/verify-parity.mjs\", [])]\nEOF\necho done", []),
    ("cat > x.py <<EOF\nnode assets/scripts/verify-parity.mjs --manifest=m\nEOF", []),
    ("python3 - <<'PY' && bash a/make-zip.sh t z\nprint('node verify-parity.mjs')\nPY", ["make-zip.sh"]),
    # unreadable quoting records nothing rather than guessing
    ("echo \"unterminated && python3 verify-wp.py", []),
    # real runs, in the shapes the skill writes them
    ("bash $S/whats-here.sh \"$WS\"; echo x; H2WP_KEY=k bash $S/allowance.sh --api=http://127.0.0.1:1",
     ["whats-here.sh", "allowance.sh"]),
    ('python3 -W ignore "$S/verify-static.py" --original a --dist b --out c', ["verify-static.py"]),
    ("python3 assets/scripts/verify-static.py --out $WS/v && node assets/scripts/verify-parity.mjs --manifest=m",
     ["verify-static.py", "verify-parity.mjs"]),
    ("assets/scripts/convert-remote.sh ws --api=x", ["convert-remote.sh"]),
    ('H2WP_WORKSPACE="$WS" bash /abs/progress.sh done -3', ["progress.sh"]),
    ("env H2WP_X=1 python3 x/make-screenshot.py --manifest=m", ["make-screenshot.py"]),
    ("timeout 60 bash x/test-env.sh up slug 2>&1 | tail -5", ["test-env.sh"]),
    ("MAKE_ZIP_MANIFEST=m.json assets/scripts/make-zip.sh theme out.zip", ["make-zip.sh"]),
    ("cd $WS/astro-project && npm install && npm run build", ["npm install", "npm run build"]),
    ("X=$(python3 a/verify-wp.py --wp u)", ["verify-wp.py"]),
    ("bash -nx a/progress.sh", []),
    ("python3 a/verify-static.py --out x && python3 a/verify-static.py --out y",
     ["verify-static.py", "verify-static.py"]),
    # the stage wrappers, in the shapes the skill writes them
    ('bash "$S/stage2-gates.sh" "$WS" --original "$WS/input-untouched" 2>&1 | tail -40', ["stage2-gates.sh"]),
    ("assets/scripts/stage3-remote.sh $WS --api=x --opts='{\"a\":1}'", ["stage3-remote.sh"]),
    ("bash a/gate-a-bisect.sh $WS --pages=a.html,b.html", ["gate-a-bisect.sh"]),
    # the Gutenberg target
    ("node assets/scripts/prepare-block-plan.mjs finalize --manifest=$WS/conversion-manifest.json",
     ["prepare-block-plan.mjs"]),
    ('python3 assets/scripts/gutenberg-verify-local.py --site="$LOCAL_WP" --edit-roundtrip --out=x',
     ["gutenberg-verify-local.py"]),
    ("python3 a/gutenberg-package.py --theme=t --report=r --out=z", ["gutenberg-package.py"]),
]
for cmd, want in RUNS:
    check(f"invocations: {cmd[:60]!r}", [n for n, _ in sp.invocations(cmd)], want)


# ------------------------------------------------------------ the hook, end to end
def hook(payload, cache):
    env = dict(os.environ, XDG_CACHE_HOME=cache)
    out = subprocess.run(["bash", os.path.join(HERE, "stage-progress.sh")], input=payload,
                         capture_output=True, text=True, env=env, timeout=30)
    check("hook exit code", out.returncode, 0)
    try:
        return json.loads(out.stdout)
    except ValueError:
        failures.append(f"hook printed non-JSON: {out.stdout!r}")
        return {}


def event(cmd, name="PostToolUse", session="s1", **extra):
    body = {"hook_event_name": name, "tool_name": "Bash", "session_id": session, "cwd": "/tmp",
            "tool_input": {"command": cmd}, "duration_ms": 1234}
    body.update(extra)
    return json.dumps(body)


with tempfile.TemporaryDirectory() as cache:
    ws = os.path.join(cache, "html2wp", "jobs", "site-1a2b3c4d")
    os.makedirs(ws)
    log = os.path.join(ws, ".h2wp-timing.jsonl")

    def rows():
        try:
            return [json.loads(line) for line in open(log)]
        except OSError:
            return []

    # Before any command names the workspace: held, then flushed in order.
    check("stage -3 before a workspace is known", hook(event("bash a/check-prereqs.sh"), cache), {})
    check("nothing written yet", rows(), [])

    out = hook(event(f"python3 a/verify-static.py --out {ws}/verify-static --key=SECRET-KEY"), cache)
    check("gate A success reminds, in the field the model reads",
          out.get("hookSpecificOutput", {}).get("hookEventName"), "PostToolUse")
    check("reminder names stage 2", "stage 2 boundary" in out["hookSpecificOutput"].get("additionalContext", ""), True)
    check("no systemMessage", "systemMessage" in json.dumps(out), False)
    check("held row flushed first, then gate A", [r.get("script") for r in rows()],
          ["check-prereqs.sh", "verify-static.py"])

    # A failed gate is a PostToolUseFailure event and still reaches the model.
    out = hook(event("python3 a/verify-wp.py --wp http://x", name="PostToolUseFailure",
                     error="Exit code 1\nB1 failed"), cache)
    check("failure reminder", "progress.sh fail 5" in out.get("hookSpecificOutput", {}).get("additionalContext", ""), True)
    check("failure row", {k: rows()[-1].get(k) for k in ("script", "ok", "exit", "ms")},
          {"script": "verify-wp.py", "ok": False, "exit": 1, "ms": 1234})

    # A Flash run (the mode progress.sh recorded in the workspace): a red gate
    # is a `warn` row and the run goes on; the reminder never asks for a rerun.
    with open(os.path.join(ws, ".h2wp-mode"), "w") as fh:
        fh.write("flash\n")
    out = hook(event("python3 a/verify-wp.py --wp http://x", name="PostToolUseFailure",
                     error="Exit code 1\nB1 failed"), cache)
    said = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("Flash reminder offers warn", "progress.sh warn 5" in said, True)
    check("Flash reminder forbids a rerun", "Never run the stage again" in said, True)
    os.remove(os.path.join(ws, ".h2wp-mode"))
    hook(event(f'H2WP_WORKSPACE="{ws}" bash a/progress.sh warn 5 "B red"'), cache)
    check("a warn call is a progress row", rows()[-1].get("event"), "warn")

    # Mentions and data write nothing and say nothing.
    before = len(rows())
    for cmd in ("grep -n x a/verify-wp.py", "python3 - <<'EOF'\nx = 'node a/verify-parity.mjs'\nEOF"):
        check(f"silent on {cmd[:30]!r}", hook(event(cmd), cache), {})
    check("mentions wrote no rows", len(rows()), before)

    # A progress call is a row, never a reminder.
    check("progress call is silent", hook(event("bash a/progress.sh done 2"), cache), {})
    check("progress row", {k: rows()[-1].get(k) for k in ("event", "stage")}, {"event": "done", "stage": "2"})

    # Launching in the background says nothing yet: the stage has not ended.
    out = hook(event("bash a/convert-remote.sh ws", tool_input={"command": "bash a/convert-remote.sh ws",
                                                               "run_in_background": True}), cache)
    check("background launch is silent", out, {})
    check("background row has no duration", ("ms" in rows()[-1], rows()[-1].get("bg")), (False, True))

    # A dry run of cleanup is not stage 7.
    check("cleanup --dry-run is silent", hook(event("bash a/cleanup.sh ws --dry-run"), cache), {})

    # Garbage in never breaks the host.
    check("not JSON", hook("not json", cache), {})
    check("not Bash", hook(json.dumps({"tool_name": "Edit", "tool_input": {}}), cache), {})

    # The command line never reaches the file: it carries paths and keys.
    text = open(log).read()
    check("no key in the timing file", "SECRET-KEY" in text, False)
    check("no URL in the timing file", "http://" in text, False)

    # Gate A run to PROVE stage 0.6 is stage 0.6's time, and is not stage 2
    # ending. (Real run: 134 s filed under stage 2 and a spurious reminder.)
    hook(event("bash a/progress.sh start 0.6"), cache)
    out = hook(event("python3 a/verify-static.py --original u --dist w"), cache)
    check("proof gate inside an open stage is silent", out, {})
    check("proof gate time belongs to the open stage", rows()[-1].get("stage"), "0.6")
    hook(event("bash a/progress.sh done 0.6"), cache)

    # The shape the skill actually writes: `H2WP_WORKSPACE="$WS"` unexpanded,
    # with `WS=…/jobs/…` earlier in the same command. The unexpanded name must
    # fall through to that, not file the call nowhere.
    out = hook(event(f'WS={ws}; H2WP_WORKSPACE="$WS" bash a/progress.sh start 0.6 && '
                     f'python3 a/verify-static.py --out "$WS/v"', session="s-fresh"), cache)
    check("unexpanded $WS: proof gate is silent", out, {})
    check("unexpanded $WS: row filed in the named workspace, under 0.6",
          (rows()[-1].get("script"), rows()[-1].get("stage")), ("verify-static.py", "0.6"))
    hook(event("bash a/progress.sh done 0.6", session="s-fresh"), cache)

    # A stage after 2 that proves itself with gate A announces ITS boundary.
    out = hook(event("bash a/progress.sh start 2.65 && python3 a/normalize-form-fields.py --apply && "
                     "python3 a/verify-static.py --out x && node a/verify-parity.mjs"), cache)
    check("2.65 proved by gate A announces 2.65", "stage 2.65 boundary" in json.dumps(out), True)
    hook(event("bash a/progress.sh done 2.65"), cache)

    # And stage 2 itself still announces stage 2.
    out = hook(event("bash a/progress.sh start 2 && python3 a/verify-static.py --out x && node a/verify-parity.mjs"),
               cache)
    check("stage 2's own gates announce stage 2", "stage 2 boundary" in json.dumps(out), True)
    hook(event("bash a/progress.sh done 2"), cache)

    # The stage wrappers end the stages they are named for — without them in
    # the boundary map, running stage 2 or 3 through a wrapper went silent.
    out = hook(event("bash a/progress.sh start 2 && bash a/stage2-gates.sh ws"), cache)
    check("stage2-gates.sh announces stage 2", "stage 2 boundary" in json.dumps(out), True)
    check("stage2-gates.sh row", {k: rows()[-1].get(k) for k in ("script", "stage", "ok")},
          {"script": "stage2-gates.sh", "stage": "2", "ok": True})
    out = hook(event("bash a/stage2-gates.sh ws", name="PostToolUseFailure", error="Exit code 9"), cache)
    check("stage2-gates.sh red says fail 2", "progress.sh fail 2" in json.dumps(out), True)
    check("stage2-gates.sh keeps its exit code", rows()[-1].get("exit"), 9)
    hook(event("bash a/progress.sh done 2"), cache)
    # A bisection of a red gate A is diagnosis: its time is stage 2's, it ends nothing.
    check("gate-a-bisect.sh is silent", hook(event("bash a/gate-a-bisect.sh ws --pages=a.html"), cache), {})
    check("gate-a-bisect.sh time is stage 2's", rows()[-1].get("stage"), "2")
    out = hook(event("python3 a/gutenberg-verify-local.py --site=http://localhost:1 --out=r"), cache)
    check("gutenberg-verify-local.py announces stage 5", "stage 5 boundary" in json.dumps(out), True)
    out = hook(event("node a/prepare-block-plan.mjs finalize --manifest=m"), cache)
    check("prepare-block-plan.mjs ends no stage", "boundary" in json.dumps(out), False)
    check("prepare-block-plan.mjs time is stage 3", (rows()[-1].get("script"), rows()[-1].get("stage")),
          ("prepare-block-plan.mjs", "3"))
    out = hook(event("bash a/stage3-remote.sh ws --api=x"), cache)
    check("stage3-remote.sh announces stage 3", "stage 3 boundary" in json.dumps(out), True)
    check("stage3-remote.sh row", (rows()[-1].get("script"), rows()[-1].get("stage")), ("stage3-remote.sh", "3"))
    out = hook(event("bash a/stage3-remote.sh ws", name="PostToolUseFailure", error="Exit code 30"), cache)
    check("stage3-remote.sh red says fail 3", "progress.sh fail 3" in json.dumps(out), True)

    # …but a LATER stage's script inside a stage somebody forgot to close is
    # the later stage starting: its own stage, and its reminder.
    hook(event("bash a/progress.sh start 2.7"), cache)
    out = hook(event("bash a/convert-remote.sh ws"), cache)
    check("upload after a forgotten done still reminds", "stage 3 boundary" in json.dumps(out), True)
    check("upload after a forgotten done keeps its stage", rows()[-1].get("stage"), "3")

    # ctrl-C is not a red gate.
    out = hook(event("python3 a/verify-wp.py --wp x", name="PostToolUseFailure", error="Interrupted",
                     is_interrupt=True), cache)
    check("interrupt says nothing", out, {})
    check("interrupt row", ("ok" in rows()[-1], rows()[-1].get("interrupted")), (False, True))

    # An `error` that is not a string must not cost the row.
    before = len(rows())
    hook(event("python3 a/verify-wp.py --wp x", name="PostToolUseFailure", error={"weird": 1}), cache)
    check("non-string error still writes its row", len(rows()), before + 1)

    # A workspace the command names outright is used, and the session's own
    # log is left alone — a test or a second site must not land in it.
    with tempfile.TemporaryDirectory() as other:
        before = len(rows())
        hook(event(f'H2WP_WORKSPACE="{other}" bash a/progress.sh done 2'), cache)
        check("named workspace: session log untouched", len(rows()), before)
        check("named workspace: mark written there",
              json.loads(open(os.path.join(other, ".h2wp-timing.jsonl")).read().splitlines()[-1])["stage"], "2")

    # A gate run twice in one call ran twice.
    hook(event("python3 a/verify-static.py --out x && python3 a/verify-static.py --out y"), cache)
    check("repeats kept in also", rows()[-1].get("also"), ["verify-static.py"])


# ------------------------------------------------------------------ progress.sh
PROGRESS = os.path.join(os.path.dirname(HERE), "skills", "html2wp", "assets", "scripts", "progress.sh")


def progress(*args, ws):
    return subprocess.run(["bash", PROGRESS, *args], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, H2WP_WORKSPACE=ws))


with tempfile.TemporaryDirectory() as ws:
    log = os.path.join(ws, ".h2wp-timing.jsonl")

    # Exit 0 — it used to exit 1 after every start/done except stage 5.
    check("progress done exits 0", progress("done", "2", ws=ws).returncode, 0)

    # A long note full of multibyte characters stays valid UTF-8 and JSON.
    progress("done", "2.5", "—" * 205, ws=ws)
    last = open(log, encoding="utf-8").read().splitlines()[-1]
    check("long multibyte note is a valid row", json.loads(last)["note"], "—" * 200)

    # One call, two writers: the script's row now, the hook's when the whole
    # Bash call ends (here: 90 s later, after a gate). Counted once — including
    # three marks from one call and a mark the hook alone saw.
    t0 = json.loads(open(log).read().splitlines()[0])["t"]
    extra = [
        {"t": t0 + 1, "event": "start", "stage": "2.5", "note": ""},
        {"t": t0 + 2, "event": "done", "stage": "2.5", "note": ""},
        {"t": t0 + 90, "event": "done", "stage": "2", "src": "hook"},
        {"t": t0 + 90, "event": "done", "stage": "2.5", "src": "hook"},
        {"t": t0 + 90, "event": "start", "stage": "2.5", "src": "hook"},
        {"t": t0 + 90, "event": "done", "stage": "2.5", "src": "hook"},
        {"t": t0 + 95, "event": "done", "stage": "3", "src": "hook"},
        {"t": t0 + 3, "event": "script", "stage": "2", "script": "verify-static.py", "ok": True, "ms": 1000},
        {"t": t0 + 4, "event": "script", "stage": "2.5", "script": "capture-chrome.py", "ok": True, "ms": 1000},
        {"t": t0 + 94, "event": "script", "stage": "3", "script": "convert-remote.sh", "ok": True, "ms": 1000},
        {"t": t0 + 96, "event": "script", "stage": "5", "script": "verify-wp.py", "ok": True, "ms": "abc"},
        {"t": t0 + 97, "event": "script", "stage": "5", "script": "smoke-editor.py", "ok": True, "ms": 1000,
         "also": "not-a-list"},
    ]
    with open(log, "a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in extra))
        fh.write("\xff not json\n")
    out = progress("summary", ws, ws=ws)
    check("summary survives malformed rows", out.returncode, 0)
    table = {line.split()[0]: line.split()[1:3] for line in out.stdout.splitlines()
             if line.strip() and line.split()[0] in ("2", "2.5", "3")}
    check("marks counted once (2, 2.5, and the hook-only 3)", table,
          {"2": ["1", "0"], "2.5": ["2", "0"], "3": ["1", "0"]})

if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"ALL OK ({len(RUNS)} commands classified, hook end to end)")
