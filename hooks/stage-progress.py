#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""
What stage-progress.sh runs. Two jobs, one pass over the hook's input.

1. THE REMINDER. When a Bash call ends a stage, tell the model which
   `progress.sh` call to make. It never says the stage passed — it cannot see
   the gate — except that a non-zero exit is itself a fact worth stating.

2. THE CLOCK. Append one row per pipeline script to
   `{workspace}/.h2wp-timing.jsonl`. Until this existed no stage duration was
   written down anywhere: progress.sh printed estimates, the conversion records
   carried one wall-clock number per site, and "which stage is slow" was an
   opinion. Claude Code already measures every tool call (`duration_ms`), so
   the clock costs nothing but reading it.

Two things this file got wrong for as long as it was a shell script, both
found by reading the hooks reference rather than by anything failing loudly:

  * It was registered under PostToolUse only, which fires after a tool
    "completes successfully". A gate that exits 1 is a FAILED tool call and
    goes to PostToolUseFailure — so the reminder to run `progress.sh fail`
    never arrived on the one occasion it exists for.
  * It put its text in `hookSpecificOutput.systemMessage`. The field that
    reaches the model is `hookSpecificOutput.additionalContext`;
    `systemMessage` is top-level and is shown to the user.

What a row holds: a timestamp, the script's file name, the stage, the
duration, whether it exited zero. NEVER the command line — that carries
paths, URLs and sometimes a licence key.

Nothing here may fail a conversion. Every path out of this file prints a JSON
object and exits 0.
"""
import json
import os
import re
import shlex
import sys
import time

# script fragment -> the stage its completion ENDS. Order matters only where
# one command could match two rows; first match wins.
#
# Absent on purpose:
#   chrome-groups.mjs   stage 2.5 ends at capture-chrome.py; firing here
#                       announced the boundary halfway through the stage.
#   compare-pages.py    composes the pictures. Stage 5.5 ends when every one
#                       has been READ, which no command marks.
#   npm run build       ends stage 1 the first time and nothing after a 2.6 or
#                       2.65 rebuild; a reminder that is wrong half the time
#                       teaches the reader to ignore it.
BOUNDARIES = [
    ("prerender-spa.py", "-1"),
    ("capture-commerce-specimen.py", "-1b"),
    ("analyze-input.mjs", "0"),
    ("optimize-images.py", "0.5"),
    ("optimize-markup.py", "0.6"),
    ("stage2-gates.sh", "2"),
    ("verify-static.py", "2"),
    ("verify-parity.mjs", "2"),
    ("capture-chrome.py", "2.5"),
    ("materialize-js-text.py", "2.6"),
    ("normalize-form-fields.py", "2.65"),
    ("detect-collections.py", "2.7"),
    ("stage3-remote.sh", "3"),
    ("convert-remote.sh", "3"),
    ("make-screenshot.py", "3.5"),
    ("gutenberg-screenshot.py", "3.5"),   # the Gutenberg target's 3.5, 5, 6
    ("verify-wp.py", "5"),
    ("smoke-editor.py", "5"),
    ("gutenberg-verify-local.py", "5"),
    ("audit-woo-coverage.py", "5.6"),
    ("make-zip.sh", "6"),
    ("gutenberg-package.py", "6"),
    ("send-verdicts.sh", "6.5"),
    ("cleanup.sh", "7"),
]

# Which stage a script's TIME belongs to. A superset of the above: these end
# nothing, but the minutes they take have to land somewhere.
ATTRIBUTION = dict(BOUNDARIES)
ATTRIBUTION.update({
    "whats-here.sh": "-4",
    "allowance.sh": "-4",
    "check-prereqs.sh": "-3",
    "mirror-live.py": "-2",
    "html-to-astro.mjs": "1",
    "chrome-groups.mjs": "2.5",
    "rebuild-theme.sh": "3",
    "test-env.sh": "5",
    "install-theme.py": "5",
    "compare-pages.py": "5.5",
    "gate-a-bisect.sh": "2",  # diagnoses a red gate A; ends nothing
    # The Gutenberg target's block plan: prepared after 2.7 and finalized by
    # the upload itself, so its minutes are stage 3's; it ends nothing.
    "prepare-block-plan.mjs": "3",
})

TIMING_FILE = ".h2wp-timing.jsonl"
# Gates the pipeline runs BEFORE their own stage, to prove an earlier one:
# gate A after 0.5, 0.6, 2.6 and 2.65; gate A2 alongside it.
PROOF_SCRIPTS = {"verify-static.py", "verify-parity.mjs"}
PROGRESS_CALL = re.compile(r"^(start|done|fail)$")
STAGE_ARG = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?b?$")
# Tokens that end one simple command and start the next. The command is
# tokenised with its quoting respected first, so a `&&` or `;` INSIDE a quoted
# string or a heredoc body is text, not a boundary — splitting on the raw
# string was how a test's own string literal `"… && node verify-parity.mjs"`
# got logged as a run of gate A2.
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n", "{", "}", "$", "|&", ";;"}
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words that run the script named after them: `python3 x.py`, `bash x.sh`,
# `node x.mjs`, and the wrappers that put one of those in front.
RUNNERS = {"python", "python3", "node", "bash", "sh", "zsh", "exec", "env", "time", "nice",
           "nohup", "command", "timeout", "gtimeout", "caffeinate", "uv", "pipx"}
WORKSPACE = re.compile(r"((?:~|\$HOME|\$\{HOME\}|/)[^\s\"'=;|&<>()]*?/html2wp/jobs/[A-Za-z0-9._-]+)")
EXIT_CODE = re.compile(r"^Exit code (\d+)")


def known_scripts():
    """The pipeline's own file names, so `foo.sh` in somebody's project is not ours."""
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.join(os.path.dirname(here), "skills", "html2wp", "assets", "scripts")
    try:
        return {n for n in os.listdir(scripts) if n.endswith((".py", ".mjs", ".sh"))} | set(ATTRIBUTION)
    except OSError:
        return set(ATTRIBUTION)


def without_heredocs(cmd):
    """The command with every heredoc BODY removed; the `<<EOF` line stays.

    A heredoc body is data handed to a program — a Python test table, a JSON
    fixture, a script piped into `python3 -` — and anything in it that looks
    like a command is not one.
    """
    kept, closing = [], []
    for line in cmd.split("\n"):
        if closing:
            if line.strip() == closing[0]:
                closing.pop(0)
            continue
        kept.append(line)
        closing = [m.group(2) for m in HEREDOC.finditer(line)]
    return "\n".join(kept)


def simple_commands(cmd):
    """The command split into its simple commands, each a list of words.

    Unbalanced quoting gives up and returns nothing: a command this cannot
    read is a command it records nothing about, which loses a row at worst
    and never invents one.
    """
    lexer = shlex.shlex(without_heredocs(cmd), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    out, words = [], []
    for token in tokens:
        if token in SEPARATORS or (token and set(token) <= set(";&|()\n")):
            if words:
                out.append(words)
            words = []
        elif token and set(token) <= set("<>"):
            words.append(token)  # a redirection; the word after it is its target
        else:
            words.append(token)
    if words:
        out.append(words)
    return out


def stage_order(stage):
    """-1 < -1b < 0 < 0.5 < … < 7; anything unreadable sorts last."""
    try:
        return (float(str(stage).rstrip("b")), str(stage).endswith("b"))
    except ValueError:
        return (float("inf"), False)


def open_stage_in(workspace):
    """The stage whose `start` is the latest progress mark, if it is still open.

    Read from the tail of the timing file: marks are written by progress.sh as
    it runs, so by the time this hook fires for a call the marks inside that
    call are already there.
    """
    if not workspace:
        return None
    try:
        with open(os.path.join(workspace, TIMING_FILE), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 32_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("event") in ("start", "done", "fail"):
            stage = r.get("stage")
            return str(stage) if r["event"] == "start" and STAGE_ARG.match(str(stage)) else None
    return None


def invocations(cmd):
    """
    The pipeline scripts this command RUNS, in order, each with its arguments.

    Not the ones it mentions. A plain substring match is what this hook did
    first, and the first real session showed the cost: `grep curl allowance.sh`
    was logged as a run of allowance.sh, and `grep … verify-wp.py` would have
    announced the stage 5 boundary. A script counts only in the position a
    shell would execute it — first word of a simple command, or the first
    script-shaped word after an interpreter — so reading, grepping or editing
    one records nothing.
    """
    found = []
    for words in simple_commands(cmd):
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        head = os.path.basename(words[0])
        if head in RUNNERS:
            rest, runner, flags = words[1:], head, set()
            for i, word in enumerate(rest):
                if word.endswith((".py", ".mjs", ".sh", ".js")):
                    # Parsing it is not running it: `bash -n`, `node --check`
                    # and `python3 -m py_compile` are the CI's syntax pass.
                    checked = (runner in ("bash", "sh", "zsh")
                               and any(re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", f) for f in flags)) \
                        or (runner == "node" and flags & {"--check", "-c"}) \
                        or (runner.startswith("python") and "-m" in flags)
                    if not checked:
                        found.append((os.path.basename(word), rest[i + 1:]))
                    break
                base = os.path.basename(word)
                inline = (runner.startswith("python") and word in ("-c", "-m")) \
                    or (runner in ("bash", "sh", "zsh") and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word)) \
                    or (runner == "node" and word in ("-e", "--eval", "-p", "--print"))
                if inline:
                    break  # the program is the next word, not a file; nothing after it is run
                if base in RUNNERS:
                    runner, flags = base, set()  # `env X=1 python3 …`, `timeout 60 bash …`
                elif word.startswith("-"):
                    flags.add(word)
                elif not ASSIGNMENT.match(word) and i > 0 and not rest[i - 1].startswith("-") \
                        and not rest[i - 1].isdigit():
                    break  # `python3 -c '…'`, `bash -lc …`: a program, not a script file
        elif head.endswith((".py", ".mjs", ".sh", ".js")):
            found.append((head, words[1:]))
        elif head == "npm" and len(words) > 1 and words[1] in ("install", "ci", "run"):
            found.append(("npm " + " ".join(words[1:3]), words[1:]))
    return found


def state_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "html2wp", ".sessions")


def sweep(directory, keep_days=30):
    """
    Each session leaves a pointer and sometimes a held-rows file here, and
    nothing else ever looks in this directory. Each file is small and bounded;
    their NUMBER was not, which is the same mistake as an unbounded file on a
    longer fuse. Run once per session, when its pointer is first written.
    """
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if name.endswith((".workspace", ".pending.jsonl")) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def workspace_for(cmd, cwd, session):
    """
    The workspace this call belongs to, remembered per session.

    Most commands name it (`--out {workspace}/verify-static`). The ones that do
    not — `progress.sh done 2` is the common case — inherit it from the last
    command in the same session that did. Keyed by session because a wave of
    three conversions on one machine is normal, and "the most recent workspace"
    would file one site's minutes under another's name.
    """
    # A workspace the command names outright wins, and is not remembered: it
    # is a one-off. Without this, `H2WP_WORKSPACE=/tmp/x progress.sh done 2`
    # — a test, a second site — fell through to the session pointer and filed
    # its mark in the real conversion's log (seen live: a reviewer's
    # reproduction landed in the run it was reviewing).
    # Only a LITERAL path counts: `H2WP_WORKSPACE="$WS"` is what the skill
    # writes, the hook sees `$WS` unexpanded, and giving up there filed every
    # such call nowhere — so an unresolvable value falls through to the search
    # below (which finds `WS=…/jobs/…` in the same command) and the pointer.
    named = re.search(r"\bH2WP_WORKSPACE=([\"']?)([^\"'\s;&|]+)\1", cmd)
    if named:
        path = os.path.expanduser(named.group(2).replace("${HOME}", "~").replace("$HOME", "~"))
        if os.path.isdir(path):
            return path

    found = None
    for text in (cmd, cwd or ""):
        m = WORKSPACE.search(text)
        if not m:
            continue
        path = m.group(1)
        for prefix in ("${HOME}", "$HOME", "~"):
            if path.startswith(prefix):
                path = os.path.expanduser("~") + path[len(prefix):]
                break
        if os.path.isdir(path):
            found = path
            break

    pointer = os.path.join(state_dir(), f"{session}.workspace") if session else None
    if found and pointer:
        try:
            os.makedirs(os.path.dirname(pointer), exist_ok=True)
            if not os.path.exists(pointer):
                sweep(os.path.dirname(pointer))
            with open(pointer, "w", encoding="utf-8") as fh:
                fh.write(found)
        except OSError:
            pass
        return found
    if found:
        return found
    if pointer:
        try:
            with open(pointer, encoding="utf-8") as fh:
                remembered = fh.read().strip()
            if os.path.isdir(remembered):
                return remembered
        except OSError:
            pass
    return None


def append(row, workspace, session):
    """
    Write the row, or hold it until the workspace is known.

    Stages -4 and -3 run before any command has named a workspace. Their rows
    wait in a per-session file and are flushed, in order, by the first row that
    knows where to go — bounded, so a session that never converts anything
    cannot grow a file forever.
    """
    line = json.dumps(row, separators=(",", ":")) + "\n"
    pending = os.path.join(state_dir(), f"{session}.pending.jsonl") if session else None
    try:
        if workspace:
            held = ""
            if pending and os.path.exists(pending):
                with open(pending, encoding="utf-8") as fh:
                    held = fh.read()
                os.remove(pending)
            with open(os.path.join(workspace, TIMING_FILE), "a", encoding="utf-8") as fh:
                fh.write(held + line)
        elif pending:
            os.makedirs(os.path.dirname(pending), exist_ok=True)
            if not os.path.exists(pending) or os.path.getsize(pending) < 64_000:
                with open(pending, "a", encoding="utf-8") as fh:
                    fh.write(line)
    except OSError:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("tool_name", "Bash") != "Bash":
        return {}
    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not isinstance(cmd, str) or not cmd:
        return {}

    event = data.get("hook_event_name") or "PostToolUse"
    failed = event == "PostToolUseFailure"
    background = tool_input.get("run_in_background") is True
    session = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id") or ""))[:80]
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    now = round(time.time(), 3)

    ours = known_scripts()
    runs = []
    for name, args in invocations(cmd):
        if name.startswith("npm "):
            # Only the Astro project's install and build are pipeline work;
            # the SPA's own `npm run build` is inside prerender-spa.py's time.
            if "astro-project" in cmd + " " + cwd:
                runs.append(("astro-build", args))
        elif name in ours:
            runs.append((name, args))
    if not runs:
        return {}
    workspace = workspace_for(cmd, cwd, session)

    # A progress call is a row, never a reminder — or it reminds you to do
    # what you just did. Only the first word after `progress.sh` decides.
    marks = [(a[0], a[1]) for n, a in runs
             if n == "progress.sh" and len(a) >= 2 and PROGRESS_CALL.match(a[0]) and STAGE_ARG.match(a[1])]
    for mode, stage in marks:
        append({"t": now, "event": mode, "stage": stage, "src": "hook"}, workspace, session)
    names = [n for n, _ in runs if n != "progress.sh"]
    if not names:
        return {}

    first = names[0]
    # Whose minutes these are. A script's own stage by name is a guess the
    # pipeline breaks on purpose: gate A runs after 0.5 and after 0.6 to prove
    # those stages, and filing those runs under stage 2 put ~4½ minutes in the
    # wrong row of a real run's summary — exactly the rows the plan reorders.
    # When a stage is open (its `start` is the latest mark), the time is that
    # stage's.
    # Only a script that belongs to that stage or an earlier one, or one of the
    # gates the pipeline deliberately runs early as PROOF of a stage, moves: a
    # later stage's script inside a stage somebody forgot to close (`done 2.7`
    # never called, then the upload) is the later stage starting, and filing
    # it — and silencing its reminder — under the stale one would be wrong.
    named = "1" if first == "astro-build" else ATTRIBUTION.get(first, "")
    current = open_stage_in(workspace)
    open_stage = current if current and (
        first in PROOF_SCRIPTS or not named or stage_order(named) <= stage_order(current)) else None
    stage = open_stage or named
    interrupted = failed and data.get("is_interrupt") is True
    row = {"t": now, "event": "script", "stage": stage, "script": first}
    if interrupted:
        row["interrupted"] = True  # stopped by the user, not a red run
    else:
        row["ok"] = not failed
    if len(names) > 1:
        row["also"] = names[1:]  # a list: a gate run twice in one call ran twice
    duration = data.get("duration_ms")
    if background:
        # Fires when the job is LAUNCHED; the number is how long starting took.
        row["bg"] = True
    elif isinstance(duration, (int, float)) and not isinstance(duration, bool):
        row["ms"] = int(duration)
    code = EXIT_CODE.match(str(data.get("error") or "")) if failed else None
    if code:
        row["exit"] = int(code.group(1))
    append(row, workspace, session)

    # The LAST boundary script the command ran: `verify-static.py … &&
    # verify-parity.mjs …` ends stage 2 once, and a command that runs a stage-2
    # gate and then the capture has reached 2.5.
    boundaries = dict(BOUNDARIES)
    # A proof gate run inside an open stage proves that stage; it does not end
    # its own. Without this, `start 2.65 && normalize && build && gate A && A2`
    # announced the stage 2 boundary instead of 2.65's.
    # Only a gate MOVED into another stage is excluded: in stage 2 itself gate A
    # is the boundary, and it still announces it.
    def proving(n):
        return n in PROOF_SCRIPTS and open_stage is not None and open_stage != ATTRIBUTION.get(n)
    ended = [n for n in names if n in boundaries and not proving(n)]
    if not ended or background or interrupted:
        return {}
    if ended[-1] == "cleanup.sh" and any("--dry-run" in a for n, args in runs if n == "cleanup.sh" for a in args):
        return {}
    boundary = boundaries[ended[-1]]
    # Gate A proving stage 0.6 is not stage 2 ending; saying so sent the model
    # to report a boundary nobody had reached.
    if open_stage and stage_order(open_stage) < stage_order(boundary):
        return {}

    if failed:
        said = f" (exit {row['exit']})" if "exit" in row else ""
        message = (
            f"html2wp: the command at the stage {boundary} boundary exited non-zero{said}. "
            f"If that was the gate's verdict, report it — "
            f"`assets/scripts/progress.sh fail {boundary} \"<why>\"`. "
            "Do not compose the line yourself."
        )
    else:
        message = (
            f"html2wp: stage {boundary} boundary reached. Report it — "
            f"`assets/scripts/progress.sh done {boundary}` if it passed, "
            f"or `assets/scripts/progress.sh fail {boundary} \"<why>\"` if it did not. "
            "Do not compose the line yourself."
        )
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


if __name__ == "__main__":
    try:
        out = main()
    except Exception:
        out = {}
    print(json.dumps(out))
    sys.exit(0)
