from geneanet_drift.capture import Date, Event, Person, Ref, Union
from geneanet_drift.gramps import (
    GrampsCitation,
    GrampsDate,
    GrampsEvent,
    GrampsFamily,
    GrampsFullPerson,
    GrampsTree,
)

SOURCE = "Geneanet - example family tree"


def ref(key: int) -> Ref:
    return Ref(key, None)


def his_date(year: int, month: int | None = None, day: int | None = None) -> Date:
    return Date(str(year), year, month, day, None, None)


def his_event(kind: str, year: int | None = None, place: str | None = None) -> Event:
    return Event(kind, his_date(year) if year else None, place)


def his_union(
    key: int, spouse: int, children=(), date: Date | None = None, place=None
) -> Union:
    return Union(
        key,
        "married",
        ref(spouse),
        date,
        place,
        tuple(ref(c) for c in children),
    )


def his(key: int, given: str, surname: str, **fields) -> Person:
    values = {
        "public_name": None,
        "other_names": (),
        "sex": "male",
        "occupation": None,
        "events": (),
        "parents": (),
        "unions": (),
        "notes": (),
        "sources": (),
        "media": (),
        "address": None,
    }
    return Person(key=key, given=given, surname=surname, **{**values, **fields})


def his_people(*people: Person) -> dict[int, Person]:
    return {p.key: p for p in people}


def gramps_date(
    year: int | None, month: int | None = None, day: int | None = None
) -> GrampsDate:
    return GrampsDate(year, month, day, None, None, "")


def gramps_event(
    kind: str, year: int | None = None, place: str | None = None, description=""
) -> GrampsEvent:
    date = gramps_date(year) if year else None
    return GrampsEvent(f"e-{kind}-{year}", kind, date, place, description)


def gramps(handle: str, given: str, surname: str, **fields) -> GrampsFullPerson:
    values = {
        "nickname": "",
        "other_names": (),
        "gender": "male",
        "events": (),
        "parent_families": (),
        "families": (),
        "notes": (),
        "citations": (),
        "media": (),
    }
    return GrampsFullPerson(
        handle, f"I-{handle}", given, surname, **{**values, **fields}
    )


def crosswalk(page: str, source: str = SOURCE) -> GrampsCitation:
    return GrampsCitation(f"c-{page}", source, page, None)


def family(handle: str, father=None, mother=None, children=(), events=()):
    return GrampsFamily(
        handle, f"F-{handle}", father, mother, tuple(children), "Married", events
    )


def tree(people, families=()) -> GrampsTree:
    return GrampsTree(tuple(people), tuple(families))
