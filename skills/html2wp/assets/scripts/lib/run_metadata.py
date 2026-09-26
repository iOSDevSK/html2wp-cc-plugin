"""Report the host's actual model selections and measured wall/turn durations."""
from datetime import datetime, timezone
import json
from pathlib import Path


def at(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError,TypeError):return None


def read(path):
    try:
        value=json.loads(Path(path).read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def metadata(ws, ended=None):
    ended=ended or datetime.now(timezone.utc).isoformat()
    stop=at(ended)
    context=read(Path(ws).parent/'.app/model-history.json')
    if context.get('schema')!='h2wp-run-context/1':context={}
    progress=read(Path(ws)/'progress.json')
    start=context.get('startedAt') or progress.get('startedAt')
    begin=at(start)
    if begin is None:start=None
    models=[]
    for row in context.get('models',[]) if isinstance(context.get('models',[]),list) else []:
        if isinstance(row,dict) and isinstance(row.get('model'),str):
            clean=lambda v: ''.join(c for c in str(v or '') if c.isalnum() or c in '-_.: ')[:160]
            models.append({'model':clean(row['model']),'effort':clean(row.get('effort')) or 'default','at':clean(row.get('at'))})
    intervals=[];incomplete=False
    for row in context.get('turns',[]) if isinstance(context.get('turns',[]),list) else []:
        if not isinstance(row,dict):continue
        left=at(row.get('startedAt'));right=at(row.get('endedAt')) or stop
        if left is not None and right is not None and right>=left:
            intervals.append((left,min(right,stop)))
            if not row.get('endedAt'):incomplete=True
    # Union intervals so simultaneous/duplicate event delivery cannot double-count.
    merged=[]
    for left,right in sorted(intervals):
        if merged and left<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],right))
        else:merged.append((left,right))
    wall=max(0,int(stop-begin)) if stop is not None and begin is not None else None
    active=int(sum(max(0,b-a) for a,b in merged)) if merged else None
    return {'models':models,'timing':{'startedAt':start,'reportedAt':ended,'wallSeconds':wall,'agentSeconds':active,'agentTimeIncludesToolsAndWaits':True,'agentTimeCoverage':'recorded turns only','openTurnAtReport':incomplete}}


def duration(seconds):
    if seconds is None:return 'Not recorded'
    hours,seconds=divmod(seconds,3600);minutes,seconds=divmod(seconds,60)
    return f'{hours}h {minutes}m {seconds}s' if hours else f'{minutes}m {seconds}s'


def section(meta):
    rows=['## Model and conversion duration','']
    if meta['models']:
        rows+=['Recorded model selections (older history may be unavailable).', '', '| Model | Reasoning effort | Selected at |','|---|---|---|']
        rows += [f"| {m['model']} | {m['effort']} | {m.get('at') or 'Not recorded'} |" for m in meta['models']]
    else:rows+=['Model: not recorded for this run.']
    timing=meta['timing']
    rows+=['',f"Total elapsed time (including pauses): **{duration(timing['wallSeconds'])}**.",f"Recorded agent time (recorded turns only, including tool execution and waits): **{duration(timing['agentSeconds'])}**."]
    if timing.get('openTurnAtReport'):rows+=['The current turn is measured up to report creation; interrupted sessions without an end event may include idle time.']
    return '\n'.join(rows)+'\n'
