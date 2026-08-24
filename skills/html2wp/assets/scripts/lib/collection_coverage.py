"""Identity matching for the independent collection coverage probe.

The loose probe deliberately ignores member attributes, while the recorded
groups use the editor's normalized class identity. Normalize only the parent
identity here so a layout utility such as ``pt-8`` cannot turn an already
recorded group into a false uncovered candidate. Member discovery remains
independent and intentionally permissive.
"""

import re


def _class_base_for_congruence(cls):
    base = cls
    while True:
        depth = 0
        separator = -1
        for index, char in enumerate(base):
            if "[" == char:
                depth += 1
            elif "]" == char and depth > 0:
                depth -= 1
            elif ":" == char and 0 == depth:
                separator = index
                break
        if separator < 0:
            break
        base = base[separator + 1:]
    return base


def _is_position_in_set_utility(cls):
    base = _class_base_for_congruence(cls).rstrip("!")
    return bool(
        re.match(r"^-?(?:col|row)-(?:span|start|end)(?:-|$)", base)
        or re.match(r"^-?order(?:-|$)", base)
        or re.match(r"^rounded-(?:t|r|b|l|s|e|tl|tr|br|bl|ss|se|ee|es)(?:-|$)", base)
        or re.match(r"^border(?:-|$)", base)
        or re.match(r"^p(?:t|r|b|l|s|e)(?:-|$)", base)
        or re.match(r"^\[(?:animation-delay|animation-duration):[^\]]+\]$", base)
    )


def normalized_parent_classes(value):
    classes = str(value or "").strip().split()
    return " ".join(sorted(
        cls for cls in classes
        if not re.match(r"^swiper-slide-(?:active|prev|next|duplicate)", cls)
        and not _is_position_in_set_utility(cls)
    ))


def uncovered(loose_groups, recorded):
    """Return loose runs not represented by a recorded strict run.

    The count remains part of the identity: an interrupted homogeneous list
    is intentionally recorded as separate runs by the editor, and a loose
    full-parent run must not hide that distinction.
    """
    seen = set()
    for group in recorded:
        seen.add((
            group["parentTag"],
            normalized_parent_classes(group["parentClasses"]),
            group["memberShape"].split("|")[0],
            group["count"],
        ))
    out = []
    for group in loose_groups:
        key = (
            group["parentTag"],
            normalized_parent_classes(group.get("parentClasses", "")),
            group["tag"],
            group["count"],
        )
        if key not in seen:
            out.append(group)
    return out
