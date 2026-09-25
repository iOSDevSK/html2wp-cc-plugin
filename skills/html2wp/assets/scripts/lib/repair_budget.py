# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Flash's repair budget: counted attempts, each one named lever.

Called by progress.sh (`repair`, `repaired`); the attempts live in
{workspace}/progress.json under `repairs`, so a new run (`mode --new`, which
puts progress.json aside) starts with the whole budget.

    repair_budget.py open  <progress.json> <result.json> <stage> <lever> <signature>
    repair_budget.py close <progress.json> <result.json> <stage> fixed|failed [note]

"No loops" and "self-repair" are compatible only as a bounded budget of named
levers, never as a re-run: a lever is one remedy from assets/repair-levers.json
for the failure a script named (`h2wp-signature: <key>`), and the stage that
failed is never started again — the repair happens inside it, before it is
closed with done, warn or fail.

The caps (from the table): 4 attempts per run, at most 2 per stage, never the
same lever on the same failure twice. A stopped run (result.json status
"stopped") is repaired only in a turn the owner started (H2WP_MODE=repair-stop):
each owner message allows 2 attempts on the stage that stopped the run, apart
from the run's own 4. A fixed attempt there puts the stopped result aside, so
the run continues from that stage.

Exit 0 = recorded (the lever's remedy on stdout); 3 = refused (why on stderr);
2 = usage.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE = HERE.parent.parent / "repair-levers.json"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write(path, doc):
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def refuse(msg):
    print("\n  " + msg.replace("\n", "\n  "), file=sys.stderr)
    return 3


def table():
    doc = read(TABLE) or {}
    return doc, doc.get("signatures") or {}, doc.get("levers") or {}


def stage_state(progress, stage):
    return next((s.get("state") for s in progress.get("stages") or []
                 if isinstance(s, dict) and s.get("stage") == stage), None)


def owner_turn(result):
    """The stopped result an owner message answers: its own time, else its
    content — a new stop is a new turn."""
    return (result or {}).get("writtenAt") or hashlib.sha1(
        json.dumps(result, sort_keys=True).encode()).hexdigest()[:12]


def open_attempt(pf, rf, stage, lever, signature):
    progress = read(pf)
    if progress is None:
        return refuse("no progress.json: a repair belongs to a run (progress.sh mode … first)")
    mode = progress.get("mode") or "full"
    if mode == "full":
        return refuse("Full mode repairs every gate until it is green by its own rules; the repair budget is Flash's.")
    meta, signatures, levers = table()
    sig = signatures.get(signature)
    if not sig:
        return refuse(f"no such failure signature '{signature}'. A script that refuses prints "
                      f"`h2wp-signature: <key>`; the keys: {', '.join(sorted(signatures))}")
    if stage not in sig.get("stages", []):
        return refuse(f"'{signature}' is a failure of stage {' or '.join(sig.get('stages', []))}, not of stage {stage}.")
    if lever not in sig.get("levers", []):
        return refuse(f"'{lever}' is not a lever for '{signature}'. Its levers, in order: "
                      f"{', '.join(sig.get('levers', []))}.")
    repairs = [r for r in progress.get("repairs") or [] if isinstance(r, dict)]
    if any(r.get("stage") == stage and r.get("outcome") == "open" for r in repairs):
        return refuse(f"a repair of stage {stage} is still open: close it first "
                      "(progress.sh repaired <stage> fixed|failed).")

    result = read(rf)
    stopped = (result or {}).get("status") == "stopped"
    owner = os.environ.get("H2WP_MODE") == "repair-stop"
    if stopped:
        at = ((result.get("stopped") or {}).get("stage"))
        if not owner:
            return refuse(f"this run stopped at stage {at} (result.json). A stopped run is repaired only in a turn "
                          "the owner started (SKILL.md, \"A stopped run — repair, then continue\").")
        if stage != at:
            return refuse(f"the run stopped at stage {at}: only that stage's levers can be spent now.")
        by, turn, cap = "owner", owner_turn(result), int(meta.get("perOwnerMessage", 2))
        mine = [r for r in repairs if r.get("by") == "owner" and r.get("turn") == turn]
        if len(mine) >= cap:
            return refuse(f"this message's {cap} repair attempts on stage {stage} are spent. Record the stop again "
                          "(write-result.py --status stopped) and tell the owner plainly what could not be fixed.")
    else:
        if owner:
            return refuse("H2WP_MODE=repair-stop is for a stopped run, and this one is not stopped.")
        if stage_state(progress, stage) != "running":
            return refuse(f"stage {stage} is not running. A repair happens INSIDE the stage that failed, before it "
                          "is closed with done, warn or fail — never as a second run of it.")
        by, turn = "run", None
        pool, per = int(meta.get("pool", 4)), int(meta.get("perStage", 2))
        mine = [r for r in repairs if r.get("by", "run") == "run"]
        if len(mine) >= pool:
            return refuse(f"the run's {pool} repair attempts are spent. Record stage {stage} red "
                          "(progress.sh warn, or fail when there is no theme and no fallback) and go on.")
        if len([r for r in mine if r.get("stage") == stage]) >= per:
            return refuse(f"stage {stage} has had its {per} repair attempts. Record it red "
                          "(progress.sh warn, or fail when there is no theme and no fallback) and go on.")
    if any(r.get("stage") == stage and r.get("signature") == signature and r.get("lever") == lever
           and r.get("by", "run") == by and (by == "run" or r.get("turn") == turn) for r in repairs):
        untried = [lv for lv in sig.get("levers", []) if not any(
            r.get("stage") == stage and r.get("signature") == signature and r.get("lever") == lv for r in repairs)]
        return refuse(f"'{lever}' was already tried on '{signature}' at stage {stage} — the same lever on the same "
                      "failure again is a loop. "
                      + (f"Next lever: {untried[0]}." if untried else "No lever is left for it: record it red."))

    n = len([r for r in repairs if r.get("stage") == stage and r.get("by", "run") == by
             and (by == "run" or r.get("turn") == turn)]) + 1
    of = int(meta.get("perOwnerMessage", 2)) if by == "owner" else int(meta.get("perStage", 2))
    row = {"stage": stage, "attempt": n, "of": of, "lever": lever, "signature": signature,
           "label": (levers.get(lever) or {}).get("label", lever), "what": sig.get("what", ""),
           "outcome": "open", "at": now(), "by": by}
    if by == "run":
        row["pool"] = len([r for r in repairs if r.get("by", "run") == "run"]) + 1
    else:
        row["turn"] = turn
    repairs.append(row)
    progress["repairs"] = repairs
    write(pf, progress)
    spec = levers.get(lever) or {}
    where = f"pool {row['pool']}/{meta.get('pool', 4)}" if by == "run" else "the owner's message"
    print(f"\n  repair {n}/{of} of stage {stage} ({where}) — {spec.get('label', lever)}")
    print(f"    remedy: {spec.get('remedy', '')}")
    if spec.get("again"):
        print(f"    then:   {spec['again']}")
    print(f"    close:  progress.sh repaired {stage} fixed|failed \"<what the check said>\"")
    return 0


def close_attempt(pf, rf, stage, outcome, note):
    if outcome not in ("fixed", "failed"):
        print("usage: progress.sh repaired <stage> fixed|failed [note]", file=sys.stderr)
        return 2
    progress = read(pf)
    repairs = [r for r in (progress or {}).get("repairs") or [] if isinstance(r, dict)]
    row = next((r for r in reversed(repairs) if r.get("stage") == stage and r.get("outcome") == "open"), None)
    if row is None:
        return refuse(f"no open repair of stage {stage} (progress.sh repair <stage> <lever> <signature> opens one).")
    row.update(outcome=outcome, note="".join(ch for ch in (note or "") if ch >= " ")[:300], closedAt=now())
    progress["repairs"] = repairs
    write(pf, progress)
    if outcome == "fixed" and row.get("by") == "owner":
        # The stop is repaired: its result is the last run's, kept beside, and
        # the run continues from the stage that stopped it.
        result = read(rf)
        if (result or {}).get("status") == "stopped":
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            os.replace(rf, str(rf)[:-len(".json")] + f"-stopped-{stamp}.json")
    word = "FIXED" if outcome == "fixed" else "still red"
    print(f"\n  repair {row['attempt']}/{row['of']} of stage {stage} — {row.get('label', row['lever'])} — {word}")
    if outcome == "fixed":
        print(f"    close the stage as usual: progress.sh done {stage}")
    else:
        print("    another lever for it (progress.sh repair …), or record the stage red and go on")
    return 0


def main(argv):
    if len(argv) >= 6 and argv[0] == "open":
        return open_attempt(argv[1], argv[2], argv[3], argv[4], argv[5])
    if len(argv) >= 5 and argv[0] == "close":
        return close_attempt(argv[1], argv[2], argv[3], argv[4], argv[5] if len(argv) > 5 else "")
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
