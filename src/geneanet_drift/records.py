"""Gramps Web API JSON for the objects the apply step writes.

Type fields carry their numeric value: the API reads a string alone as the
default type.
"""

import hashlib
from datetime import date

GENDER_VALUES = {"female": 0, "male": 1, "unknown": 2}
MODIFIER_VALUES = {None: 0, "before": 1, "after": 2, "about": 3}
QUALITY_VALUES = {None: 0, "estimated": 1, "calculated": 2}
EVENT_TYPES = {
    "marriage": 1,
    "birth": 12,
    "death": 13,
    "burial": 19,
    "occupation": 37,
}
CUSTOM = 0
UNKNOWN = -1
REPOSITORY_WEBSITE = 6
CONFIDENCE_LOW = 1
NAME_AKA = 1
FAMILY_MARRIED = 0
FAMILY_UNKNOWN = 3
ROLE_PRIMARY = 1
ROLE_FAMILY = 8
NOTE_GENERAL = 1
NOTE_PERSON = 4
NOTE_FAMILY = 9
NOTE_EVENT = 10
ENDPOINTS = {
    "Person": "people",
    "Family": "families",
    "Event": "events",
    "Place": "places",
    "Citation": "citations",
    "Note": "notes",
    "Source": "sources",
    "Repository": "repositories",
    "Media": "media",
}


def handle_for(*parts: str) -> str:
    """A stable handle, so a plan names the objects it creates before creating them."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"gd{digest[:25]}"


def typed(kind: str, value: int, string: str = "") -> dict:
    return {"_class": kind, "value": value, "string": string}


def _date(day: int, month: int, year: int, modifier: int, quality: int) -> dict:
    return {
        "_class": "Date",
        "calendar": 0,
        "modifier": modifier,
        "quality": quality,
        "dateval": [day, month, year, False],
        "text": "",
        "newyear": 0,
    }


def date_json(value: dict) -> dict:
    return _date(
        value.get("day") or 0,
        value.get("month") or 0,
        value["year"] or 0,
        MODIFIER_VALUES[value.get("modifier")],
        QUALITY_VALUES[value.get("quality")],
    )


def day_json(day: date) -> dict:
    return _date(day.day, day.month, day.year, 0, 0)


def event_type(kind: str) -> dict:
    if kind in EVENT_TYPES:
        return typed("EventType", EVENT_TYPES[kind])
    return typed("EventType", CUSTOM, kind.capitalize())


def event(
    handle: str,
    kind: str,
    when: dict | None = None,
    place: str | None = None,
    description: str = "",
    citations: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict:
    return {
        "_class": "Event",
        "handle": handle,
        "type": event_type(kind),
        "date": date_json(when) if when else _date(0, 0, 0, 0, 0),
        "place": place or "",
        "description": description,
        "citation_list": citations or [],
        "note_list": notes or [],
    }


def event_ref(handle: str, family: bool = False) -> dict:
    role = typed("EventRoleType", ROLE_FAMILY if family else ROLE_PRIMARY)
    return {"_class": "EventRef", "ref": handle, "role": role}


def child_ref(handle: str) -> dict:
    return {"_class": "ChildRef", "ref": handle}


def media_ref(handle: str) -> dict:
    return {"_class": "MediaRef", "ref": handle}


def name(given: str, surname: str, alternate: bool = False) -> dict:
    return {
        "_class": "Name",
        "first_name": given,
        "surname_list": [{"_class": "Surname", "surname": surname, "primary": True}],
    } | ({"type": typed("NameType", NAME_AKA)} if alternate else {})


def alternate_name(full: str) -> dict:
    *given, surname = full.split()
    return name(" ".join(given), surname, alternate=True)


def person(handle: str, given: str, surname: str, gender: str, **fields) -> dict:
    return {
        "_class": "Person",
        "handle": handle,
        "gender": GENDER_VALUES[gender],
        "primary_name": name(given, surname),
        **fields,
    }


def family(
    handle: str,
    father: str | None,
    mother: str | None,
    children: list[str],
    events: list[dict],
    **fields,
) -> dict:
    kind = FAMILY_MARRIED if events else FAMILY_UNKNOWN
    return {
        "_class": "Family",
        "handle": handle,
        "father_handle": father,
        "mother_handle": mother,
        "child_ref_list": [child_ref(c) for c in children],
        "event_ref_list": events,
        "type": typed("FamilyRelType", kind),
        **fields,
    }


def citation(handle: str, source: str, page: str, url: str, accessed: date) -> dict:
    return {
        "_class": "Citation",
        "handle": handle,
        "source_handle": source,
        "page": page,
        "confidence": CONFIDENCE_LOW,
        "date": day_json(accessed),
        "attribute_list": [
            {
                "_class": "Attribute",
                "type": typed("SrcAttributeType", CUSTOM, "URL"),
                "value": url,
            }
        ],
    }


def note(handle: str, text: str, kind: int) -> dict:
    return {
        "_class": "Note",
        "handle": handle,
        "text": {"_class": "StyledText", "string": text, "tags": []},
        "type": typed("NoteType", kind),
    }


def place(handle: str, label: str, parent: str | None) -> dict:
    refs = [{"_class": "PlaceRef", "ref": parent}] if parent else []
    return {
        "_class": "Place",
        "handle": handle,
        "name": {"_class": "PlaceName", "value": label},
        "place_type": typed("PlaceType", UNKNOWN),
        "placeref_list": refs,
    }


def repository(handle: str, label: str) -> dict:
    return {
        "_class": "Repository",
        "handle": handle,
        "name": label,
        "type": typed("RepositoryType", REPOSITORY_WEBSITE),
    }


def source(handle: str, title: str, repository_handle: str) -> dict:
    return {
        "_class": "Source",
        "handle": handle,
        "title": title,
        "reporef_list": [{"_class": "RepoRef", "ref": repository_handle}],
    }
