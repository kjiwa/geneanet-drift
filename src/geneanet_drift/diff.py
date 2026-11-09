import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass

from geneanet_drift.capture import Event, Person, Ref, Union
from geneanet_drift.gramps import (
    GrampsEvent,
    GrampsFamily,
    GrampsFullPerson,
    GrampsTree,
)
from geneanet_drift.match import normalise
from geneanet_drift.match_tree import CANDIDATES, LINKED, NEW, Match, display_name

NEW_VALUE = "new"
DIFFERS = "differs"
MISSING_IN_HIS = "missing_in_his"
NEW_PERSON = "new_person"
SENSITIVE_NOTE_KINDS = ("contact", "private")
EVENT_KINDS = {
    "born": "birth",
    "deceased": "death",
    "buried": "burial",
    "killed": "killed",
}
DATE_PARTS = ("year", "month", "day")


@dataclass(frozen=True)
class Change:
    id: str
    person: int
    field: str
    kind: str
    his: object
    mine: object
    proposed: object
    sensitive: bool = False


@dataclass(frozen=True)
class Context:
    people: dict[int, Person]
    mine: dict[str, GrampsFullPerson]
    families: dict[str, GrampsFamily]
    matches: dict[int, Match]


def change_id(person: int, field: str, his: object) -> str:
    payload = json.dumps([person, field, his], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _change(
    person: int, field: str, kind: str, his, mine, proposed, sensitive: bool = False
) -> Change:
    return Change(
        change_id(person, field, his),
        person,
        field,
        kind,
        his,
        mine,
        proposed,
        sensitive,
    )


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def _date_value(date) -> dict | None:
    if date is None:
        return None
    return {
        "year": date.year,
        "month": date.month,
        "day": date.day,
        "modifier": date.modifier,
        "quality": date.quality,
    }


def _fact(date, place: str | None) -> dict:
    return {"date": _date_value(date), "place": place}


def _refines(fine: dict, coarse: dict) -> bool:
    return (
        fine != coarse
        and fine["modifier"] == coarse["modifier"]
        and fine["quality"] == coarse["quality"]
        and all(coarse[p] is None or coarse[p] == fine[p] for p in DATE_PARTS)
    )


def _proposed_date(his: dict | None, mine: dict | None) -> dict | None:
    if his is None or (mine is not None and _refines(mine, his)):
        return mine
    return his


def _date_differs(his: dict | None, mine: dict | None) -> bool:
    return his is not None and _proposed_date(his, mine) != mine


def _place_differs(his: str | None, mine: str | None) -> bool:
    return bool(his) and not set(normalise(his).split()) <= set(
        normalise(mine or "").split()
    )


def _is_blank(fact: dict | None) -> bool:
    return fact is None or (fact["date"] is None and not fact["place"])


def _fact_changes(person: int, field: str, his: dict | None, mine: dict | None):
    if _is_blank(his) and mine is None:
        return
    if mine is None:
        yield _change(person, field, NEW_VALUE, his, None, his)
    elif his is None:
        yield _change(person, field, MISSING_IN_HIS, None, mine, None)
    elif _is_blank(his):
        return
    elif _date_differs(his["date"], mine["date"]) or _place_differs(
        his["place"], mine["place"]
    ):
        proposed = {
            "date": _proposed_date(his["date"], mine["date"]),
            "place": his["place"] or mine["place"],
        }
        yield _change(person, field, DIFFERS, his, mine, proposed)


def _scalar_changes(person: int, field: str, his, mine):
    his, mine = his or None, mine or None
    if his == mine:
        return
    if mine is None:
        yield _change(person, field, NEW_VALUE, his, None, his)
    elif his is None:
        yield _change(person, field, MISSING_IN_HIS, None, mine, None)
    else:
        yield _change(person, field, DIFFERS, his, mine, his)


def _first_event(events, kind: str):
    return next((e for e in events if e.kind.casefold() == kind), None)


def _his_event(events: tuple[Event, ...], kind: str) -> Event | None:
    return next((e for e in events if e.kind == kind), None)


def _mine_fact(event: GrampsEvent | None) -> dict | None:
    return _fact(event.date, event.place) if event else None


def _event_changes(person: Person, mine: GrampsFullPerson) -> Iterator[Change]:
    for his_kind, kind in EVENT_KINDS.items():
        event = _his_event(person.events, his_kind)
        his = _fact(event.date, event.place) if event else None
        yield from _fact_changes(
            person.key,
            f"event:{his_kind}",
            his,
            _mine_fact(_first_event(mine.events, kind)),
        )


def _occupation_changes(person: Person, mine: GrampsFullPerson):
    event = _first_event(mine.events, "occupation")
    yield from _scalar_changes(
        person.key,
        "occupation",
        person.occupation,
        event.description if event else None,
    )


def _gender_changes(person: Person, mine: GrampsFullPerson):
    def known(sex: str) -> str | None:
        return None if sex == "unknown" else sex

    yield from _scalar_changes(
        person.key, "gender", known(person.sex), known(mine.gender)
    )


def _name_changes(person: Person, mine: GrampsFullPerson):
    yield from _scalar_changes(person.key, "given", person.given, mine.given)
    yield from _scalar_changes(person.key, "surname", person.surname, mine.surname)


def _other_name_changes(person: Person, mine: GrampsFullPerson):
    known = {normalise(n) for n in (*mine.other_names, f"{mine.given} {mine.surname}")}
    for name in person.other_names:
        if normalise(name) not in known:
            yield _change(person.key, "other_name", NEW_VALUE, name, None, name)


def _is_sensitive(kind: str) -> bool:
    return any(word in kind.casefold() for word in SENSITIVE_NOTE_KINDS)


def _note_changes(person: Person, mine: GrampsFullPerson):
    known = [_squash(n.text) for n in mine.notes]
    for note in person.notes:
        if not any(_squash(note.text) in text for text in known):
            value = {"kind": note.kind, "text": note.text}
            yield _change(
                person.key,
                "note",
                NEW_VALUE,
                value,
                None,
                value,
                _is_sensitive(note.kind),
            )


def _media_changes(person: Person, mine: GrampsFullPerson):
    known = {normalise(m.description) for m in mine.media}
    for media in person.media:
        if not media.title or normalise(media.title) not in known:
            value = {
                "id": media.id,
                "title": media.title,
                "date": media.date,
                "original": media.original,
            }
            yield _change(person.key, "media", NEW_VALUE, value, None, value)


def _source_changes(person: Person, mine: GrampsFullPerson):
    known = [_squash(f"{c.source} {c.page}") for c in mine.citations]
    for source in person.sources:
        if not any(_squash(source.text) in text for text in known):
            value = {"fact": source.fact, "text": source.text}
            yield _change(person.key, "source", NEW_VALUE, value, None, value)


def _real(ctx: Context, ref: Ref) -> bool:
    """False for placeholders and for links to people outside the capture."""
    return ref.key in ctx.people


def _marriage(family: GrampsFamily | None) -> dict:
    event = _first_event(family.events, "marriage") if family else None
    return _mine_fact(event) or _fact(None, None)


def _ref(ctx: Context, key: int) -> dict:
    match = ctx.matches[key]
    return {"handle": match.handle} if match.kind == LINKED else {"key": key}


def _name_of(ctx: Context, key: int) -> str:
    return display_name(ctx.people[key])


def _handle_name(ctx: Context, handle: str) -> str:
    mine = ctx.mine[handle]
    return f"{mine.given} {mine.surname}".strip()


def _parent_handles(ctx: Context, mine: GrampsFullPerson) -> set[str]:
    families = (ctx.families[h] for h in mine.parent_families)
    return {p for f in families for p in (f.father, f.mother) if p}


def _parent_changes(ctx: Context, person: Person, mine: GrampsFullPerson):
    present = _parent_handles(ctx, mine)
    seen: set[str] = set()
    for ref in person.parents:
        if not _real(ctx, ref):
            continue
        match = ctx.matches[ref.key]
        seen.add(match.handle or "")
        if match.handle not in present:
            yield _change(
                person.key,
                f"parent:{ref.key}",
                NEW_VALUE,
                _name_of(ctx, ref.key),
                None,
                _ref(ctx, ref.key),
            )
    for handle in sorted(present - seen):
        field = f"parent:{ctx.mine[handle].gramps_id}"
        yield _change(
            person.key, field, MISSING_IN_HIS, None, _handle_name(ctx, handle), None
        )


def _spouse_handle(ctx: Context, union: Union) -> str | None:
    return ctx.matches[union.spouse.key].handle if _real(ctx, union.spouse) else None


def _family_for(ctx: Context, union: Union, mine: GrampsFullPerson, claimed: set[str]):
    handle = _spouse_handle(ctx, union)
    return next(
        (
            f
            for f in (ctx.families[h] for h in mine.families)
            if handle and handle in (f.father, f.mother) and f.handle not in claimed
        ),
        None,
    )


def _union_view(ctx: Context, union: Union) -> dict:
    return {
        "spouse": _name_of(ctx, union.spouse.key) if _real(ctx, union.spouse) else None,
        "date": _date_value(union.date),
        "place": union.place,
        "children": [_name_of(ctx, c.key) for c in union.children if _real(ctx, c)],
    }


def _family_view(ctx: Context, family: GrampsFamily, handle: str) -> dict:
    spouse = next(
        (p for p in (family.father, family.mother) if p not in (handle, None)), None
    )
    return {
        "spouse": _handle_name(ctx, spouse) if spouse else None,
        **_marriage(family),
        "children": [_handle_name(ctx, c) for c in family.children],
    }


def _union_value(ctx: Context, union: Union, family: GrampsFamily | None) -> dict:
    marriage = _marriage(family)
    kept = [{"handle": c} for c in family.children] if family else []
    his_children = [_ref(ctx, c.key) for c in union.children if _real(ctx, c)]
    children = his_children + [c for c in kept if c not in his_children]
    return {
        "spouse": _ref(ctx, union.spouse.key) if _real(ctx, union.spouse) else None,
        "date": _proposed_date(_date_value(union.date), marriage["date"]),
        "place": union.place or marriage["place"],
        "children": children,
    }


def _union_differs(ctx: Context, union: Union, family: GrampsFamily) -> bool:
    marriage = _marriage(family)
    present = set(family.children)
    return (
        _date_differs(_date_value(union.date), marriage["date"])
        or _place_differs(union.place, marriage["place"])
        or any(
            ctx.matches[c.key].handle not in present
            for c in union.children
            if _real(ctx, c)
        )
    )


def _union_changes(ctx: Context, person: Person, mine: GrampsFullPerson):
    used: set[str] = set()
    for union in person.unions:
        family = _family_for(ctx, union, mine, used)
        field = f"union:{union.key}"
        his = _union_view(ctx, union)
        proposed = _union_value(ctx, union, family)
        if family is None:
            yield _change(person.key, field, NEW_VALUE, his, None, proposed)
            continue
        used.add(family.handle)
        if _union_differs(ctx, union, family):
            view = _family_view(ctx, family, mine.handle)
            yield _change(person.key, field, DIFFERS, his, view, proposed)
    for handle in mine.families:
        if handle not in used:
            family = ctx.families[handle]
            field = f"union:{family.gramps_id}"
            view = _family_view(ctx, family, mine.handle)
            yield _change(person.key, field, MISSING_IN_HIS, None, view, None)


def _linked_changes(ctx: Context, person: Person, match: Match) -> Iterator[Change]:
    mine = ctx.mine[match.handle]
    yield from _name_changes(person, mine)
    yield from _other_name_changes(person, mine)
    yield from _gender_changes(person, mine)
    yield from _event_changes(person, mine)
    yield from _occupation_changes(person, mine)
    yield from _note_changes(person, mine)
    yield from _media_changes(person, mine)
    yield from _source_changes(person, mine)
    yield from _parent_changes(ctx, person, mine)
    yield from _union_changes(ctx, person, mine)


def _link_changes(ctx: Context, person: Person, match: Match) -> Iterator[Change]:
    for handle in match.candidates:
        mine = ctx.mine[handle]
        yield _change(
            person.key,
            f"link:{mine.gramps_id}",
            NEW_VALUE,
            display_name(person),
            f"{_handle_name(ctx, handle)} [{mine.gramps_id}]",
            {"handle": handle},
        )


def _bundle(ctx: Context, person: Person) -> dict:
    return {
        "given": person.given,
        "surname": person.surname,
        "gender": person.sex,
        "other_names": list(person.other_names),
        "occupation": person.occupation,
        "events": {
            e.kind: _fact(e.date, e.place)
            for e in person.events
            if not _is_blank(_fact(e.date, e.place))
        },
        "notes": [{"kind": n.kind, "text": n.text} for n in person.notes],
        "media": [
            {"id": m.id, "title": m.title, "date": m.date, "original": m.original}
            for m in person.media
        ],
        "sources": [{"fact": s.fact, "text": s.text} for s in person.sources],
        "parents": [_ref(ctx, r.key) for r in person.parents if _real(ctx, r)],
        "unions": [_union_value(ctx, u, None) for u in person.unions],
    }


def _new_person_change(ctx: Context, person: Person) -> Change:
    bundle = _bundle(ctx, person)
    sensitive = any(_is_sensitive(n.kind) for n in person.notes)
    return _change(person.key, "person", NEW_PERSON, bundle, None, bundle, sensitive)


def _person_changes(ctx: Context, person: Person) -> Iterator[Change]:
    match = ctx.matches[person.key]
    if match.kind == LINKED:
        yield from _linked_changes(ctx, person, match)
    elif match.kind == CANDIDATES:
        yield from _link_changes(ctx, person, match)
    elif match.kind == NEW:
        yield _new_person_change(ctx, person)


def diff_people(
    people: dict[int, Person], tree: GrampsTree, matches: dict[int, Match]
) -> list[Change]:
    ctx = Context(
        people,
        {p.handle: p for p in tree.people},
        {f.handle: f for f in tree.families},
        matches,
    )
    return [c for key in sorted(people) for c in _person_changes(ctx, people[key])]
