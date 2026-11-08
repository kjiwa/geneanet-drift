from dataclasses import dataclass
from pathlib import Path

from geneanet_drift.capture import MONTHS, Person, load_tree
from geneanet_drift.config import Config
from geneanet_drift.decisions import ACCEPTED, EDITED, LINK_PREFIX, Decision
from geneanet_drift.diff import (
    DIFFERS,
    MISSING_IN_HIS,
    NEW_PERSON,
    NEW_VALUE,
    Change,
    diff_people,
)
from geneanet_drift.gramps import GrampsFullPerson, GrampsTree
from geneanet_drift.match import load_negatives
from geneanet_drift.match_tree import (
    LINKED,
    Match,
    display_name,
    gramps_year,
    match_people,
)
from geneanet_drift.review import source_title
from geneanet_drift.tree_file import read_tree

TEXT, NOTE, FACT = "text", "note", "fact"
TEXT_FIELDS = ("given", "surname", "occupation", "other_name")
FIELD_LABELS = {
    "given": "Given name",
    "surname": "Surname",
    "gender": "Gender",
    "occupation": "Occupation",
    "other_name": "Other name",
    "note": "Note",
    "media": "Photo or document",
    "source": "Source",
    "person": "New person",
    "event:born": "Birth",
    "event:deceased": "Death",
    "event:buried": "Burial",
    "event:killed": "Killed",
    "parent": "Parent",
    "union": "Marriage or partnership",
    "link": "Possible match",
}
PHOTO_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MAX_TEXT = 2000
CHOSEN_WHY = "chosen by you"


@dataclass(frozen=True)
class Review:
    people: dict[int, Person]
    tree: GrampsTree
    matches: dict[int, Match]
    suggestions: list[Change]
    media_dir: Path | None


def load_review(cfg: Config, pages_dir: Path, snapshot: Path) -> Review:
    people = load_tree(pages_dir, cfg.tree)
    tree = read_tree(snapshot)
    matches = match_people(
        people, tree, source_title(cfg.tree), load_negatives(cfg.negatives_path)
    )
    media = pages_dir.parent / "media"
    return Review(
        people,
        tree,
        matches,
        diff_people(people, tree, matches),
        media if media.is_dir() else None,
    )


def is_link(change: Change) -> bool:
    return change.field.startswith(LINK_PREFIX)


def link_changes(review: Review) -> list[Change]:
    return [c for c in review.suggestions if is_link(c)]


def chosen_links(review: Review, decisions: dict[str, Decision]) -> dict[int, Change]:
    return {
        c.person: c
        for c in link_changes(review)
        if decisions.get(c.id) == Decision(ACCEPTED)
    }


def field_changes(review: Review, decisions: dict[str, Decision]) -> list[Change]:
    """Field-level changes, with every link the operator accepted treated as matched."""
    chosen = {
        key: Match(LINKED, link.proposed["handle"], why=CHOSEN_WHY)
        for key, link in chosen_links(review, decisions).items()
    }
    changes = (
        diff_people(review.people, review.tree, {**review.matches, **chosen})
        if chosen
        else review.suggestions
    )
    return [c for c in changes if not is_link(c)]


def known_ids(review: Review, decisions: dict[str, Decision]) -> set[str]:
    return {c.id for c in (*field_changes(review, decisions), *link_changes(review))}


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field) or FIELD_LABELS[field.split(":")[0]]


def date_text(date: dict | None) -> str:
    if not date or date.get("year") is None:
        return ""
    year = date["year"]
    month = MONTHS[date["month"] - 1] if date.get("month") else None
    if month and date.get("day"):
        core = f"{month} {date['day']}, {year}"
    elif month:
        core = f"{month} {year}"
    else:
        core = str(year)
    return " ".join(p for p in (date.get("modifier"), core) if p)


def _fact_text(fact: dict) -> str:
    return ", ".join(p for p in (date_text(fact["date"]), fact["place"]) if p)


def _union_lines(union: dict) -> list[str]:
    lines = [f"Spouse: {union['spouse'] or 'unknown'}"]
    if marriage := _fact_text(union):
        lines.append(f"Married: {marriage}")
    if union["children"]:
        lines.append(f"Children: {', '.join(union['children'])}")
    return lines


def value_lines(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if "spouse" in value:
        return _union_lines(value)
    if "date" in value and "place" in value:
        return [_fact_text(value)]
    if "kind" in value and "text" in value:
        return [f"{value['kind']}: {value['text']}"]
    if "fact" in value:
        return [f"{value['fact']}: {value['text']}"]
    if "title" in value:
        return [p for p in (value["title"] or "Untitled", value["date"]) if p]
    return []


def person_lines(person: Person) -> list[str]:
    lines = [display_name(person), f"Gender: {person.sex}"]
    if person.occupation:
        lines.append(f"Occupation: {person.occupation}")
    for event in person.events:
        parts = (event.date and event.date.text, event.place)
        if text := ", ".join(p for p in parts if p):
            lines.append(f"{event.kind.capitalize()}: {text}")
    for count, noun in (
        (len(person.parents), "parent"),
        (len(person.unions), "union"),
        (len(person.notes), "note"),
        (len(person.media), "photo or document"),
    ):
        if count:
            lines.append(f"{count} {noun}{'' if count == 1 else 's'} recorded")
    return lines


def editable_kind(change: Change) -> str | None:
    if change.kind not in (NEW_VALUE, DIFFERS):
        return None
    if change.field in TEXT_FIELDS:
        return TEXT
    if change.field == "note":
        return NOTE
    if change.field.startswith("event:"):
        return FACT
    return None


def _clean_text(value, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f"{what} must be text of up to {MAX_TEXT} characters")
    return value.strip()


def validate_edit(kind: str | None, value):
    if kind in (TEXT, NOTE):
        if not _clean_text(value, "the value"):
            raise ValueError("enter a value")
        return value.strip()
    if kind == FACT and isinstance(value, dict):
        date = _clean_text(value.get("date_text", ""), "the date")
        place = _clean_text(value.get("place", ""), "the place")
        if not (date or place):
            raise ValueError("enter a date or a place")
        return {"date_text": date, "place": place}
    raise ValueError("this change cannot be edited")


def edit_lines(kind: str | None, value) -> list[str]:
    if kind == FACT:
        return [", ".join(p for p in (value["date_text"], value["place"]) if p)]
    return [value]


def edit_seed(change: Change, kind: str | None):
    if kind == FACT:
        date, place = change.proposed["date"], change.proposed["place"]
        return {"date_text": date_text(date), "place": place or ""}
    if kind == NOTE:
        return change.proposed["text"]
    return change.proposed


def _photo(review: Review, change: Change) -> str | None:
    if change.field != "media" or review.media_dir is None:
        return None
    folder = review.media_dir / f"person-{change.person}"
    stem = str(change.proposed["id"])
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        named = path.stem == stem or path.stem.startswith(f"{stem}-")
        if named and path.suffix.lower() in PHOTO_TYPES:
            return f"/media/{change.person}/{path.name}"
    return None


def _decision_view(decision: Decision | None) -> dict | None:
    return {"status": decision.status, "value": decision.value} if decision else None


def _proposed_lines(change: Change) -> list[str] | None:
    refs = change.field.startswith(("union", "parent"))
    if change.kind != DIFFERS or refs or change.proposed == change.his:
        return None
    return value_lines(change.proposed)


def _his_lines(review: Review, change: Change) -> list[str]:
    if change.kind == NEW_PERSON:
        return person_lines(review.people[change.person])
    return value_lines(change.mine if is_link(change) else change.his)


def row_view(review: Review, change: Change, decisions: dict[str, Decision]) -> dict:
    kind = editable_kind(change)
    decision = decisions.get(change.id)
    edited = decision.value if decision and decision.status == EDITED else None
    return {
        "id": change.id,
        "label": field_label(change.field),
        "kind": change.kind,
        "his": _his_lines(review, change),
        "mine": value_lines(change.mine),
        "proposed": _proposed_lines(change),
        "sensitive": change.sensitive,
        "informational": change.kind == MISSING_IN_HIS,
        "editable": kind,
        "edit_seed": edit_seed(change, kind) if kind else None,
        "edit_lines": edit_lines(kind, edited) if edited is not None else None,
        "decision": _decision_view(decision),
        "photo": _photo(review, change),
    }


def life_text(born: int | None, died: int | None) -> str:
    parts = ([f"born {born}"] if born else []) + ([f"died {died}"] if died else [])
    return ", ".join(parts)


def _his_years(person: Person) -> str:
    def year(kind: str) -> int | None:
        found = (e.date.year for e in person.events if e.kind == kind and e.date)
        return next(found, None)

    return life_text(year("born"), year("deceased"))


def _mine_name(person: GrampsFullPerson) -> str:
    return f"{person.given} {person.surname}".strip()


def _candidate_view(
    mine: GrampsFullPerson, change: Change, decisions: dict[str, Decision]
) -> dict:
    return {
        "change": change.id,
        "gramps_id": mine.gramps_id,
        "name": _mine_name(mine),
        "years": life_text(gramps_year(mine, "birth"), gramps_year(mine, "death")),
        "decision": _decision_view(decisions.get(change.id)),
    }


def _state(rows: list[dict], matched: bool, new: bool) -> str:
    if new:
        return "new"
    if not matched:
        return "needs_match"
    return "differs" if any(not r["informational"] for r in rows) else "unchanged"


def _linked_handle(review: Review, key: int, chosen: dict[int, Change]) -> str | None:
    if key in chosen:
        return chosen[key].proposed["handle"]
    return review.matches[key].handle


def people_view(review: Review, decisions: dict[str, Decision]) -> list[dict]:
    chosen = chosen_links(review, decisions)
    by_person: dict[int, list[Change]] = {}
    for change in field_changes(review, decisions):
        by_person.setdefault(change.person, []).append(change)
    mine_by_handle = {p.handle: p for p in review.tree.people}
    links = link_changes(review)
    result = []
    for key, person in sorted(review.people.items()):
        rows = [row_view(review, c, decisions) for c in by_person.get(key, [])]
        handle = _linked_handle(review, key, chosen)
        new = any(c.kind == NEW_PERSON for c in by_person.get(key, []))
        actionable = [r for r in rows if not r["informational"]]
        found = mine_by_handle.get(handle)
        result.append(
            {
                "key": key,
                "name": display_name(person),
                "years": _his_years(person),
                "state": _state(rows, handle is not None, new),
                "mine": found and {"id": found.gramps_id, "name": _mine_name(found)},
                "why": CHOSEN_WHY if key in chosen else review.matches[key].why,
                "changes": rows,
                "candidates": [
                    _candidate_view(mine_by_handle[c.proposed["handle"]], c, decisions)
                    for c in links
                    if c.person == key
                ],
                "actionable": len(actionable),
                "decided": sum(1 for r in actionable if r["decision"]),
            }
        )
    return result


def accepted_changes(
    review: Review, decisions: dict[str, Decision]
) -> list[tuple[Change, Decision]]:
    every = [*link_changes(review), *field_changes(review, decisions)]
    return [
        (c, decisions[c.id])
        for c in every
        if c.id in decisions and decisions[c.id].status in (ACCEPTED, EDITED)
    ]


def _verb(change: Change) -> str:
    if change.kind == NEW_PERSON:
        return "Create person"
    if is_link(change):
        return "Link to existing person"
    return "Add" if change.kind == NEW_VALUE else "Change"


def preview_view(review: Review, decisions: dict[str, Decision]) -> dict:
    items = []
    for change, decision in accepted_changes(review, decisions):
        edited = decision.status == EDITED
        items.append(
            {
                "person": display_name(review.people[change.person]),
                "action": _verb(change),
                "label": field_label(change.field),
                "lines": edit_lines(editable_kind(change), decision.value)
                if edited
                else _his_lines(review, change),
                "edited": edited,
                "sensitive": change.sensitive,
            }
        )
    return {"count": len(items), "items": items}


def summary_view(people: list[dict], accepted: int) -> dict:
    def count(state: str) -> int:
        return sum(1 for p in people if p["state"] == state)

    return {
        "people": len(people),
        "needs_match": count("needs_match"),
        "new": count("new"),
        "differs": count("differs"),
        "unchanged": count("unchanged"),
        "accepted": accepted,
    }
