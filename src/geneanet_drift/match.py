import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from geneanet_drift.config import read_json, write_json
from geneanet_drift.feed import Entry, format_lifespan, name_key
from geneanet_drift.gramps import GrampsPerson, Snapshot

# Edits closer together than this are one piece of work.
CLUSTER_GAP = timedelta(days=7)
NAME_SIMILARITY = 0.9
MIN_CONTAINED_WORDS = 2
YEAR_TOLERANCE = 1
# Every other change label is reported as a notice, never turned into work.
ACTIONABLE_CHANGES = frozenset(
    {
        "Individual added",
        "Individual updated",
        "Family added",
        "Family updated",
        "Parents added",
    }
)

LINKED = "linked"
CONFIRMED = "confirmed"
PROBABLE = "probable"
AMBIGUOUS = "ambiguous"
NONE = "none"
NEEDS_DECISION = (PROBABLE, AMBIGUOUS)


@dataclass(frozen=True)
class WorkItem:
    name: str
    url: str
    key: str
    changes: tuple[tuple[datetime, str], ...]  # newest first
    note: str = ""
    birth_year: int | None = None
    death_year: int | None = None

    @property
    def newest(self) -> datetime:
        return self.changes[0][0]

    @property
    def surname(self) -> str:
        return self.name.split()[-1]

    @property
    def history(self) -> str:
        return "; ".join(
            f"{change} {when.date().isoformat()}" for when, change in self.changes
        )

    @property
    def lifespan(self) -> str:
        return format_lifespan(self.birth_year, self.death_year)


@dataclass(frozen=True)
class Notice:
    entry: Entry
    person: GrampsPerson | None  # his person's counterpart, when already linked


@dataclass(frozen=True)
class Verdict:
    person: GrampsPerson
    agree: str
    conflict: str


@dataclass(frozen=True)
class Resolution:
    item: WorkItem
    tier: str
    reason: str
    candidates: tuple[GrampsPerson, ...] = ()
    person: GrampsPerson | None = None


@dataclass(frozen=True)
class Cluster:
    resolutions: tuple[Resolution, ...]
    anchor: Resolution | None
    relevant: bool


def normalise(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    letters = re.sub(r"[^a-z ]", "", re.sub(r"[-.]", " ", ascii_name.casefold()))
    letters = re.sub(r"(?:tch|kch|ch)", "ch", letters.replace("q", "k"))
    return re.sub(r"(.)\1+", r"\1", " ".join(letters.split()))


def _same_name(wanted: str, other: str) -> bool:
    if SequenceMatcher(None, wanted, other).ratio() >= NAME_SIMILARITY:
        return True
    # Patronymic chains: one tree may record "Given Father" where the other
    # records "Given Father Grandfather".
    shorter, longer = sorted((set(wanted.split()), set(other.split())), key=len)
    return len(shorter) >= MIN_CONTAINED_WORDS and shorter <= longer


def name_candidates(name: str, people: tuple[GrampsPerson, ...]) -> list[GrampsPerson]:
    wanted = normalise(name)
    return [p for p in people if _same_name(wanted, normalise(p.name))]


def is_placeholder(name: str) -> bool:
    """GeneWeb records an unknown parent or spouse as '?' (or '?.0')."""
    return any(re.sub(r"\.\d+$", "", token) == "?" for token in name.split())


def _is_work(entry: Entry) -> bool:
    return entry.change in ACTIONABLE_CHANGES and not is_placeholder(entry.name)


def _first_year(group: list[Entry], field: str) -> int | None:
    return next(
        (getattr(e, field) for e in group if getattr(e, field) is not None), None
    )


def work_items(entries: list[Entry]) -> list[WorkItem]:
    grouped: dict[str, list[Entry]] = {}
    for entry in sorted(entries, key=lambda e: e.when, reverse=True):
        if _is_work(entry):
            grouped.setdefault(name_key(entry.name), []).append(entry)
    return [
        WorkItem(
            name=group[0].name,
            url=group[0].url,
            key=key,
            changes=tuple((e.when, e.change) for e in group),
            birth_year=_first_year(group, "birth_year"),
            death_year=_first_year(group, "death_year"),
        )
        for key, group in grouped.items()
    ]


def build_notices(entries: list[Entry], snapshot: Snapshot) -> list[Notice]:
    by_id = {p.gramps_id: p for p in snapshot.people}
    return [
        Notice(e, by_id.get(snapshot.links.get(name_key(e.name), "")))
        for e in sorted(entries, key=lambda e: e.when, reverse=True)
        if not _is_work(e)
    ]


def combine(fresh: list[WorkItem], pending: list[WorkItem]) -> list[WorkItem]:
    by_key = {item.key: item for item in fresh}
    for item in pending:
        current = by_key.get(item.key)
        changes = sorted(
            {*item.changes, *(current.changes if current else ())}, reverse=True
        )
        by_key[item.key] = replace(
            current or item, changes=tuple(changes), note=item.note
        )
    return list(by_key.values())


def _split_by_gap(items: list[WorkItem]) -> list[list[WorkItem]]:
    groups: list[list[WorkItem]] = []
    for item in sorted(items, key=lambda i: i.newest, reverse=True):
        if groups and groups[-1][-1].newest - item.newest <= CLUSTER_GAP:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _resolve_cluster(
    items: list[WorkItem],
    snapshot: Snapshot,
    negatives: set[tuple[str, str]],
    accepted: dict[str, str],
    priority: set[str],
) -> Cluster:
    by_id = {p.gramps_id: p for p in snapshot.people}
    known: dict[str, Resolution] = {}
    for item in items:
        if item.key in snapshot.links:
            known[item.key] = Resolution(
                item,
                LINKED,
                "linked in Gramps by citation",
                person=by_id[snapshot.links[item.key]],
            )
        elif item.key in accepted:
            known[item.key] = Resolution(
                item, CONFIRMED, "accepted by you", person=by_id[accepted[item.key]]
            )
    anchor = next(iter(known.values()), None)

    resolutions = [
        known[item.key]
        if item.key in known
        else _resolve_unknown(item, snapshot, negatives, anchor)
        for item in items
    ]
    relevant = any(normalise(item.surname) in priority for item in items)
    return Cluster(tuple(resolutions), anchor, relevant)


def _year_verdict(item: WorkItem, person: GrampsPerson) -> Verdict:
    pairs = (
        ("birth", item.birth_year, person.birth_year),
        ("death", item.death_year, person.death_year),
    )
    known = [(label, his, yours) for label, his, yours in pairs if his and yours]
    agree = [
        f"{label} year agrees ({his})"
        for label, his, yours in known
        if abs(his - yours) <= YEAR_TOLERANCE
    ]
    conflict = [
        f"{label} {his} vs {yours}"
        for label, his, yours in known
        if abs(his - yours) > YEAR_TOLERANCE
    ]
    return Verdict(person, "; ".join(agree), "; ".join(conflict))


def _narrow(verdicts: list[Verdict]) -> list[Verdict]:
    agreeing = [v for v in verdicts if v.agree and not v.conflict]
    if agreeing:
        return agreeing
    return [v for v in verdicts if not v.conflict] or verdicts


def _resolve_single(
    item: WorkItem, verdict: Verdict, anchor: Resolution | None, suffix: str
) -> Resolution:
    person = verdict.person
    if verdict.conflict:
        reason = f"name match, but years differ: {verdict.conflict} (his vs yours)"
        return Resolution(item, PROBABLE, reason + suffix, (person,))
    if verdict.agree:
        return Resolution(
            item, CONFIRMED, f"name match; {verdict.agree}{suffix}", (person,), person
        )
    if anchor is None:
        return Resolution(item, PROBABLE, "name match only" + suffix, (person,))
    reason = f"name match; cluster anchored at {anchor.person.gramps_id}{suffix}"
    return Resolution(item, CONFIRMED, reason, (person,), person)


def _resolve_unknown(
    item: WorkItem,
    snapshot: Snapshot,
    negatives: set[tuple[str, str]],
    anchor: Resolution | None,
) -> Resolution:
    candidates = [
        p
        for p in name_candidates(item.name, snapshot.people)
        if (item.key, p.gramps_id) not in negatives
    ]
    if not candidates:
        return Resolution(item, NONE, "no name match in Gramps")
    pool = _narrow([_year_verdict(item, p) for p in candidates])
    if len(pool) > 1:
        return Resolution(
            item, AMBIGUOUS, "several name matches", tuple(v.person for v in pool)
        )
    suffix = "; other name matches do not agree on years" if len(candidates) > 1 else ""
    return _resolve_single(item, pool[0], anchor, suffix)


def build_clusters(
    items: list[WorkItem],
    snapshot: Snapshot,
    negatives: set[tuple[str, str]],
    accepted: dict[str, str],
    priority_surnames: tuple[str, ...],
) -> list[Cluster]:
    priority = {normalise(s) for s in priority_surnames}
    clusters = [
        _resolve_cluster(group, snapshot, negatives, accepted, priority)
        for group in _split_by_gap(items)
    ]
    clusters = _note_shared_claims(clusters)
    return sorted(clusters, key=lambda c: not c.relevant)


def _note_shared_claims(clusters: list[Cluster]) -> list[Cluster]:
    """Two of his entries pointing at one of your people is worth a second look."""
    claimed = {
        r.person.gramps_id: r.item.name
        for c in clusters
        for r in c.resolutions
        if r.person is not None
    }

    def annotate(resolution: Resolution) -> Resolution:
        if resolution.tier not in NEEDS_DECISION:
            return resolution
        taken = [
            f"{p.gramps_id} is already matched to his {claimed[p.gramps_id]!r}"
            for p in resolution.candidates
            if claimed.get(p.gramps_id, resolution.item.name) != resolution.item.name
        ]
        if not taken:
            return resolution
        return replace(resolution, reason=f"{resolution.reason}; {'; '.join(taken)}")

    return [
        replace(c, resolutions=tuple(annotate(r) for r in c.resolutions))
        for c in clusters
    ]


def needing_decision(clusters: list[Cluster]) -> list[Resolution]:
    return [r for c in clusters for r in c.resolutions if r.tier in NEEDS_DECISION]


def load_negatives(path: Path) -> set[tuple[str, str]]:
    return {(row["his"], row["gramps"]) for row in read_json(path, [])}


def save_negatives(path: Path, negatives: set[tuple[str, str]]) -> None:
    write_json(
        path, [{"his": his, "gramps": gramps} for his, gramps in sorted(negatives)]
    )


def load_pending(path: Path) -> list[WorkItem]:
    return [
        WorkItem(
            name=row["name"],
            url=row["url"],
            key=name_key(row["name"]),
            changes=tuple((datetime.fromisoformat(w), c) for w, c in row["changes"]),
            note=row["note"],
            birth_year=row.get("birth_year"),
            death_year=row.get("death_year"),
        )
        for row in read_json(path, [])
    ]


def save_pending(path: Path, pending: list[WorkItem]) -> None:
    write_json(
        path,
        [
            {
                "name": i.name,
                "url": i.url,
                "note": i.note,
                "birth_year": i.birth_year,
                "death_year": i.death_year,
                "changes": [[w.isoformat(), c] for w, c in i.changes],
            }
            for i in pending
        ],
    )
