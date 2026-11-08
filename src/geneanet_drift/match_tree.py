from collections import Counter
from dataclasses import dataclass

from geneanet_drift.capture import Person
from geneanet_drift.feed import name_key
from geneanet_drift.gramps import GrampsFullPerson, GrampsPerson, GrampsTree
from geneanet_drift.match import (
    Verdict,
    WorkItem,
    name_candidates,
    narrow,
    year_verdict,
)

LINKED = "linked"
CANDIDATES = "candidates"
NEW = "new"
ROLES = ("parents", "spouses", "children")
CONTRADICTING_ROLES = ("parents", "spouses")


@dataclass(frozen=True)
class Match:
    kind: str
    handle: str | None = None
    candidates: tuple[str, ...] = ()
    why: str = ""


@dataclass(frozen=True)
class Relatives:
    parents: frozenset
    spouses: frozenset
    children: frozenset


def display_name(person: Person) -> str:
    return f"{person.given} {person.surname}".strip()


def _name_keys(person: Person) -> set[str]:
    names = {display_name(person), person.public_name or ""}
    return {name_key(n) for n in names if n}


def _his_year(person: Person, kind: str) -> int | None:
    return next((e.date.year for e in person.events if e.kind == kind and e.date), None)


def gramps_year(person: GrampsFullPerson, kind: str) -> int | None:
    return next(
        (e.date.year for e in person.events if e.kind.casefold() == kind and e.date),
        None,
    )


def _gramps_person(person: GrampsFullPerson) -> GrampsPerson:
    return GrampsPerson(
        person.handle,
        person.gramps_id,
        person.given,
        person.surname,
        gramps_year(person, "birth"),
        gramps_year(person, "death"),
    )


def _item(person: Person) -> WorkItem:
    return WorkItem(
        name=display_name(person),
        url="",
        key=str(person.key),
        changes=(),
        birth_year=_his_year(person, "born"),
        death_year=_his_year(person, "deceased"),
    )


def _his_relatives(person: Person) -> Relatives:
    def keys(refs) -> frozenset:
        return frozenset(r.key for r in refs if r.key is not None)

    return Relatives(
        keys(person.parents),
        keys(u.spouse for u in person.unions),
        keys(c for u in person.unions for c in u.children),
    )


def _gramps_relatives(tree: GrampsTree) -> dict[str, Relatives]:
    families = {f.handle: f for f in tree.families}
    found = {}
    for person in tree.people:
        parents, spouses, children = set(), set(), set()
        for handle in person.parent_families:
            parents |= {families[handle].father, families[handle].mother} - {None}
        for handle in person.families:
            family = families[handle]
            spouses |= {family.father, family.mother} - {None, person.handle}
            children |= set(family.children)
        found[person.handle] = Relatives(
            frozenset(parents), frozenset(spouses), frozenset(children)
        )
    return found


def _claims(
    people: dict[int, Person], tree: GrampsTree, source: str, negatives
) -> dict[int, str]:
    """Citations under the Geneanet source name the person by his display name."""
    pages: dict[str, set[str]] = {}
    for mine in tree.people:
        for citation in mine.citations:
            if citation.source == source and citation.page:
                pages.setdefault(name_key(citation.page), set()).add(mine.handle)
    holders = Counter(k for p in people.values() for k in _name_keys(p))
    ids = {p.handle: p.gramps_id for p in tree.people}
    claims: dict[int, set[str]] = {}
    for key, person in people.items():
        for name in _name_keys(person):
            handles = pages.get(name, set())
            if holders[name] == 1 and len(handles) == 1:
                (handle,) = handles
                if (name_key(display_name(person)), ids[handle]) not in negatives:
                    claims.setdefault(key, set()).add(handle)
    return {k: next(iter(h)) for k, h in claims.items() if len(h) == 1}


def _crosswalk(people, tree, source, negatives) -> dict[int, tuple[str, str]]:
    claims = _claims(people, tree, source, negatives)
    sharing = Counter(claims.values())
    return {
        key: (handle, "citation under the Geneanet source")
        for key, handle in claims.items()
        if sharing[handle] == 1
    }


def _pools(people, tree, negatives, matched) -> dict[int, list[Verdict]]:
    gramps = tuple(_gramps_person(p) for p in tree.people)
    ids = {p.handle: p.gramps_id for p in tree.people}
    taken = {handle for handle, _ in matched.values()}
    pools = {}
    for key, person in people.items():
        if key in matched:
            continue
        found = [
            c
            for c in name_candidates(display_name(person), gramps)
            if (name_key(display_name(person)), ids[c.handle]) not in negatives
            and c.handle not in taken
        ]
        pools[key] = narrow([year_verdict(_item(person), c) for c in found])
    return pools


def _resolved(his: Relatives, matched: dict[int, tuple[str, str]]) -> Relatives:
    def handles(keys) -> frozenset:
        return frozenset(matched[k][0] for k in keys if k in matched)

    return Relatives(*(handles(getattr(his, role)) for role in ROLES))


def _support(his: Relatives, mine: Relatives) -> int:
    return sum(len(getattr(his, r) & getattr(mine, r)) for r in ROLES)


def _contradicts(his: Relatives, mine: Relatives) -> bool:
    return any(
        getattr(his, r) and getattr(mine, r) and not getattr(his, r) & getattr(mine, r)
        for r in CONTRADICTING_ROLES
    )


def _usable(pool, his, relatives, taken) -> list[Verdict]:
    return [
        v
        for v in pool
        if v.person.handle not in taken
        and not _contradicts(his, relatives[v.person.handle])
    ]


def _pick(usable, his, relatives) -> tuple[str, str] | None:
    scored = [(_support(his, relatives[v.person.handle]), v) for v in usable]
    best = max((s for s, _ in scored), default=0)
    top = [v for s, v in scored if s == best]
    if len(top) != 1 or top[0].conflict:
        return None
    if best > 0:
        return top[0].person.handle, f"{best} relatives already matched"
    if len(usable) == 1 and top[0].agree:
        return top[0].person.handle, f"name match; {top[0].agree}"
    return None


def _round(pools, matched, relatives, graph) -> dict[int, tuple[str, str]]:
    taken = {handle for handle, _ in matched.values()}
    proposals = {}
    for key, pool in pools.items():
        if key in matched:
            continue
        his = _resolved(graph[key], matched)
        picked = _pick(_usable(pool, his, relatives, taken), his, relatives)
        if picked:
            proposals[key] = picked
    contested = Counter(handle for handle, _ in proposals.values())
    return {k: v for k, v in proposals.items() if contested[v[0]] == 1}


def _unmatched(pool, his, relatives, taken) -> Match:
    usable = _usable(pool, his, relatives, taken)
    if not usable:
        return Match(NEW, why="no name match in Gramps")
    conflicts = [v.conflict for v in usable if v.conflict]
    reason = f"years differ: {'; '.join(conflicts)}" if conflicts else "name match only"
    handles = tuple(v.person.handle for v in usable)
    return Match(CANDIDATES, candidates=handles, why=reason)


def match_people(
    people: dict[int, Person],
    tree: GrampsTree,
    source: str,
    negatives: set[tuple[str, str]],
) -> dict[int, Match]:
    matched = _crosswalk(people, tree, source, negatives)
    pools = _pools(people, tree, negatives, matched)
    relatives = _gramps_relatives(tree)
    graph = {key: _his_relatives(p) for key, p in people.items()}
    while found := _round(pools, matched, relatives, graph):
        matched.update(found)
    taken = {handle for handle, _ in matched.values()}
    return {key: _final(key, matched, pools, graph, relatives, taken) for key in people}


def _final(key, matched, pools, graph, relatives, taken) -> Match:
    if key in matched:
        handle, why = matched[key]
        return Match(LINKED, handle, why=why)
    return _unmatched(pools[key], _resolved(graph[key], matched), relatives, taken)
