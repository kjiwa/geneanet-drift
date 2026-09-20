from datetime import date, datetime

from geneanet_drift.match import (
    CONFIRMED,
    LINKED,
    NONE,
    Cluster,
    Notice,
    Resolution,
    WorkItem,
    is_placeholder,
)

CHECKBOX = "[ ] "
INDENT = "    "
CITATION_INDENT = INDENT + " " * len("Citation: ")
ADVICE = {
    "Picture added": "a picture was added to {who}; view it on his page if you want the image",
    "Picture deleted": "a picture was deleted from {who}",
    "Individual deleted": "he deleted {who}; check whether yours should follow",
}
PLACEHOLDER = (
    '"{change}" on an unknown person ({who}); nothing to add unless he names them'
)
UNRECOGNISED = 'unrecognised change "{change}" on {who}; check his page'


def source_title(tree: str) -> str:
    return f"Geneanet - {tree} family tree"


def _citation_block(tree: str, item: WorkItem, today: date) -> list[str]:
    return [
        f'{INDENT}Citation: Source "{source_title(tree)}"',
        f'{CITATION_INDENT}page = "{item.name}"   date = {today.isoformat()} (accessed)',
        f"{CITATION_INDENT}(the name exactly as his change log shows it)",
        f"{CITATION_INDENT}confidence = Low",
        f"{CITATION_INDENT}attribute URL = copy the address from the address bar of his page",
        f"{CITATION_INDENT}attach to: the person (the link read next run),",
        f"{CITATION_INDENT}also each name, event or relationship imported from his page",
    ]


def _note_block(text: str) -> list[str]:
    return [f'{INDENT}Note:     (Person Note) "{text}"']


def _linked_entry(resolution: Resolution) -> list[str]:
    person = resolution.person
    return [
        f"{CHECKBOX}Check {person.name} [{person.gramps_id}]   (his tree: {resolution.item.history})",
        f"{INDENT}Compare: {resolution.item.url}&lang=en",
    ]


def _confirmed_entry(tree: str, resolution: Resolution, today: date) -> list[str]:
    person, item = resolution.person, resolution.item
    note = f"Matched to {person.gramps_id} ({person.name}): {resolution.reason}."
    return [
        f"{CHECKBOX}Update {person.name} [{person.gramps_id}]   (his tree: {item.history})",
        f"{INDENT}Compare: {item.url}&lang=en",
        *_citation_block(tree, item, today),
        *_note_block(note),
    ]


def _new_entry(tree: str, resolution: Resolution, today: date) -> list[str]:
    item = resolution.item
    return [
        f"{CHECKBOX}Add person {item.name}   (his tree: {item.history})",
        f"{INDENT}Compare: {item.url}&lang=en",
        *_citation_block(tree, item, today),
        *_note_block(f"Added from the {tree} Geneanet tree; {resolution.reason}."),
    ]


def _entry(tree: str, resolution: Resolution, today: date) -> list[str]:
    if resolution.tier == LINKED:
        return _linked_entry(resolution)
    if resolution.tier == CONFIRMED:
        return _confirmed_entry(tree, resolution, today)
    return _new_entry(tree, resolution, today)


def _heading(number: int, cluster: Cluster) -> str:
    tiers = [r.tier for r in cluster.resolutions]
    new = tiers.count(NONE)
    where = (
        f"anchored at {cluster.anchor.person.gramps_id} {cluster.anchor.person.name}"
        if cluster.anchor
        else "no anchor"
    )
    return f"## Cluster {number} - {where}: {new} new, {len(tiers) - new} updates"


def _deferred_section(deferred: list[WorkItem]) -> list[str]:
    lines = ["## Deferred (resurfaces next run; nothing to apply)", ""]
    for item in deferred:
        note = f": {item.note}" if item.note else ""
        lines.append(f"- {item.name}{note}   {item.url}&lang=en")
    return lines


def _notice_line(notice: Notice) -> str:
    entry, person = notice.entry, notice.person
    who = f"{entry.name} [{person.gramps_id}]" if person else entry.name
    template = PLACEHOLDER if is_placeholder(entry.name) else ADVICE.get(entry.change)
    advice = (template or UNRECOGNISED).format(who=who, change=entry.change)
    return f"- {entry.when.date().isoformat()}  {advice}"


def _notice_section(notices: list[Notice]) -> list[str]:
    return ["## Informational (nothing to tick)", "", *map(_notice_line, notices), ""]


def render(
    tree: str,
    clusters: list[Cluster],
    notices: list[Notice],
    deferred: list[WorkItem],
    today: date,
    after: datetime,
) -> str:
    lines = [
        f"# Geneanet drift review - {today.isoformat()}",
        "",
        f"Tree: {tree}   Changes after: {after.isoformat(sep=' ', timespec='seconds')}",
        "",
    ]
    for number, cluster in enumerate(clusters, start=1):
        lines += [_heading(number, cluster), ""]
        for resolution in cluster.resolutions:
            lines += [*_entry(tree, resolution, today), ""]
    if notices:
        lines += _notice_section(notices)
    if deferred:
        lines += _deferred_section(deferred)
    return "\n".join(lines).rstrip() + "\n"


def unticked(review: str) -> int:
    return sum(line.startswith(CHECKBOX) for line in review.splitlines())
