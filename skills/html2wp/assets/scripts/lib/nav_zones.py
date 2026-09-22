"""Where a manifest nav entry's zone is, in the order a gate should look.

make-theme stamps every zone it wires with data-ve-nav="{n}" (n = the
entry's 1-based position) in every artifact, and writes that selector back as
`zoneSelector` — into the SERVICE's copy of the manifest, which the local
gates never see. Falling straight back to the authored selector measured the
wrong element whenever entries share a class: three footer columns authored
as "div.flex-col.gap-3" were each checked as the first column.
"""


def nav_zone_candidates(entry, index):
    out = []
    for sel in (entry.get("zoneSelector"), f'[data-ve-nav="{index + 1}"]', entry.get("selector")):
        if sel and sel not in out:
            out.append(sel)
    return out or [""]
