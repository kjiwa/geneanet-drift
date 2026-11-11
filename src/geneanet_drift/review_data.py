import shlex
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from geneanet_drift.apply import source_title
from geneanet_drift.capture import MONTHS, Person, load_tree
from geneanet_drift.config import Config
from geneanet_drift.decisions import (
    ACCEPTED,
    EDITED,
    PLACE_PREFIX,
    Decision,
    link_negatives,
    refused_people,
)
from geneanet_drift.diff import (
    DIFFERS,
    LINK_PREFIX,
    MISSING_IN_HIS,
    NEW_PERSON,
    NEW_VALUE,
    Change,
    diff_people,
    skip_reason,
)
from geneanet_drift.feed import page_url
from geneanet_drift.gramps import GrampsFullPerson, GrampsTree
from geneanet_drift.match import load_negatives, normalise
from geneanet_drift.match_tree import (
    CANDIDATES,
    LINKED,
    NEW,
    Match,
    display_name,
    gramps_year,
    match_people,
)
from geneanet_drift.tree_file import THUMBNAILS_DIR, read_tree

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
MARKERS = {CANDIDATES: " (no match yet)", NEW: " (new)"}


@dataclass(frozen=True)
class Review:
    people: dict[int, Person]
    tree: GrampsTree
    source: str
    negatives: frozenset[tuple[str, str]]
    media_dir: Path | None
    thumbnails_dir: Path | None
    tree_name: str
    gramps_url: str
    pages: Path


@dataclass(frozen=True)
class Derived:
    matches: dict[int, Match]
    changes: list[Change]


def load_review(cfg: Config, pages_dir: Path, snapshot: Path) -> Review:
    return Review(
        load_tree(pages_dir, cfg.tree),
        read_tree(snapshot),
        source_title(cfg.tree),
        frozenset(load_negatives(cfg.negatives_path)),
        pages_dir.parent / "media",
        snapshot.parent / THUMBNAILS_DIR,
        cfg.tree,
        cfg.gramps_url,
        pages_dir,
    )


def is_link(change: Change) -> bool:
    return change.field.startswith(LINK_PREFIX)


def _accepted_links(changes: list[Change], decisions) -> dict[int, str]:
    """The first accepted link of each person, in change order."""
    found: dict[int, str] = {}
    for c in changes:
        if is_link(c) and decisions.get(c.id) == Decision(ACCEPTED):
            found.setdefault(c.person, c.proposed["handle"])
    return found


def place_mapping(review: Review, decisions: dict[str, Decision]) -> dict[str, str]:
    """His place text to the title of the Gramps place chosen for it."""
    titles = {p.handle: p.title for p in review.tree.places}
    return {
        key.removeprefix(PLACE_PREFIX): titles[decision.value]
        for key, decision in decisions.items()
        if key.startswith(PLACE_PREFIX) and decision.value in titles
    }


def derive(review: Review, decisions: dict[str, Decision]) -> Derived:
    """Match and diff again with every accepted link chosen and rejected one negative.

    A person's first choice stands and negatives only grow, so every pass but the
    last adds a choice or a negative; the cap turns a broken invariant into an error.
    """
    chosen: dict[int, str] = {}
    places = place_mapping(review, decisions)
    negatives = set(review.negatives)
    refused = refused_people(decisions)
    passes = len(review.people) * (len(review.tree.people) + 1) + 2
    for _ in range(passes):
        matches = match_people(
            review.people, review.tree, review.source, negatives, chosen, refused
        )
        changes = diff_people(review.people, review.tree, matches, places)
        found = {**_accepted_links(changes, decisions), **chosen}
        rejected = negatives | link_negatives(changes, decisions)
        if found == chosen and rejected == negatives:
            stable = _stable_candidates(review, negatives, chosen, refused, matches)
            if stable is not matches:
                changes = diff_people(review.people, review.tree, stable, places)
            return Derived(stable, changes)
        chosen, negatives = found, rejected
    raise RuntimeError(f"review matching did not settle in {passes} passes")


def _stable_candidates(
    review: Review,
    negatives: set[tuple[str, str]],
    chosen: dict[int, str],
    refused: frozenset[int],
    matches: dict[int, Match],
) -> dict[int, Match]:
    """Offer a chosen person the candidates seen before any choice, plus the choice.

    Relatives matched because of a choice would otherwise add candidates.
    """
    if not chosen:
        return matches
    baseline = match_people(
        review.people, review.tree, review.source, negatives, refused=refused
    )
    return {
        key: replace(
            match,
            candidates=tuple(dict.fromkeys([*baseline[key].candidates, chosen[key]])),
        )
        if key in chosen
        else match
        for key, match in matches.items()
    }


def link_changes(review: Review, decisions: dict[str, Decision]) -> list[Change]:
    return [c for c in derive(review, decisions).changes if is_link(c)]


def field_changes(review: Review, decisions: dict[str, Decision]) -> list[Change]:
    return [c for c in derive(review, decisions).changes if not is_link(c)]


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field) or FIELD_LABELS[field.split(":")[0]]


def _day_text(year, month, day) -> str:
    name = MONTHS[month - 1] if month else None
    if name and day:
        return f"{name} {day}, {year}"
    return f"{name} {year}" if name else str(year)


def _core_text(date: dict) -> str:
    start = _day_text(date["year"], date.get("month"), date.get("day"))
    if not date.get("end_year"):
        return start
    end = _day_text(date["end_year"], date.get("end_month"), date.get("end_day"))
    if date.get("modifier") == "span":
        return f"from {start} to {end}"
    return f"between {start} and {end}"


def date_text(date: dict | None) -> str:
    if not date:
        return ""
    if date.get("modifier") == "text":
        return date.get("text", "")
    if date.get("year") is None:
        return ""
    modifier = date.get("modifier")
    words = [date.get("quality")]
    if modifier in ("before", "after", "about"):
        words.append(modifier)
    words.append(_core_text(date))
    return " ".join(w for w in words if w)


def _fact_text(fact: dict) -> str:
    return ", ".join(p for p in (date_text(fact["date"]), fact["place"]) if p)


def _union_lines(union: dict) -> list[str]:
    lines = [f"Spouse: {union['spouse'] or 'unknown'}"]
    if marriage := _fact_text(union):
        lines.append(f"Married: {marriage}")
    if union["children"]:
        lines.append(f"Children: {', '.join(union['children'])}")
    return lines


def _existing_media_lines(held: list[dict]) -> list[str]:
    if not held:
        return ["No photos in Gramps."]
    return [f"Already in Gramps: {', '.join(m['title'] for m in held)}"]


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
    if "existing" in value:
        return _existing_media_lines(value["existing"])
    if "title" in value:
        return [p for p in (value["title"] or "Untitled", value["date"]) if p]
    return []


def _relative_text(review: Review, key: int, matches: dict[int, Match]) -> str:
    match = matches[key]
    if match.kind == LINKED:
        mine = next(p for p in review.tree.people if p.handle == match.handle)
        return f"{display_name(review.people[key])} (in Gramps: {mine.gramps_id})"
    return _marked(display_name(review.people[key]), key, matches)


def _relative_texts(review: Review, refs, matches: dict[int, Match]) -> list[str]:
    return [
        _relative_text(review, r.key, matches) for r in refs if r.key in review.people
    ]


def _relation_lines(
    review: Review, person: Person, matches: dict[int, Match]
) -> list[str]:
    lines = [
        f"Parent: {text}" for text in _relative_texts(review, person.parents, matches)
    ]
    for union in person.unions:
        lines += [
            f"Spouse: {text}"
            for text in _relative_texts(review, [union.spouse], matches)
        ]
        if children := _relative_texts(review, union.children, matches):
            lines.append(f"Children: {', '.join(children)}")
    return lines


def person_lines(
    review: Review, person: Person, matches: dict[int, Match]
) -> list[str]:
    lines = [display_name(person), f"Gender: {person.sex}"]
    if person.occupation:
        lines.append(f"Occupation: {person.occupation}")
    for event in person.events:
        parts = (event.date and event.date.text, event.place)
        if text := ", ".join(p for p in parts if p):
            lines.append(f"{event.kind.capitalize()}: {text}")
    lines += _relation_lines(review, person, matches)
    for count, noun in (
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


def _mine_photos(review: Review, change: Change) -> list[dict]:
    held = change.mine.get("existing", []) if change.field == "media" else []
    if review.thumbnails_dir is None:
        return []
    return [
        {"url": f"/thumbnails/{m['handle']}.jpg", "alt": m["title"]}
        for m in held
        if (review.thumbnails_dir / f"{m['handle']}.jpg").is_file()
    ]


def _decision_view(decision: Decision | None) -> dict | None:
    return {"status": decision.status, "value": decision.value} if decision else None


def _proposed_lines(change: Change) -> list[str] | None:
    refs = change.field.startswith(("union", "parent"))
    if change.kind != DIFFERS or refs or change.proposed == change.his:
        return None
    return value_lines(change.proposed)


def _marked(name: str, key: int, matches: dict[int, Match]) -> str:
    return name + MARKERS.get(matches[key].kind, "")


def _marked_union(review: Review, change: Change, matches: dict[int, Match]) -> dict:
    his = change.his
    key = int(change.field.split(":")[1])
    union = next(u for u in review.people[change.person].unions if u.key == key)
    real = [r for r in (union.spouse, *union.children) if r.key in review.people]
    names = iter(
        _marked(display_name(review.people[r.key]), r.key, matches) for r in real
    )
    spouse = next(names) if his["spouse"] is not None else None
    return {**his, "spouse": spouse, "children": list(names)}


def _marked_his(review: Review, change: Change, matches: dict[int, Match]):
    kind, _, suffix = change.field.partition(":")
    if change.kind not in (NEW_VALUE, DIFFERS) or kind not in ("parent", "union"):
        return change.his
    if kind == "parent":
        return _marked(change.his, int(suffix), matches)
    return _marked_union(review, change, matches)


def _his_lines(review: Review, change: Change, matches: dict[int, Match]) -> list[str]:
    if change.kind == NEW_PERSON:
        return person_lines(review, review.people[change.person], matches)
    if is_link(change):
        return value_lines(change.mine)
    return value_lines(_marked_his(review, change, matches))


def _waiting_name(review: Review, change: Change) -> str | None:
    if change.waiting_on is None:
        return None
    return display_name(review.people[change.waiting_on])


def _his_place_title(change: Change, places: dict[str, str]) -> str | None:
    """The Gramps title chosen for the place text on his side of a fact row."""
    his = change.his
    place = his.get("place") if isinstance(his, dict) else None
    return places.get(place) if place else None


def row_view(
    review: Review,
    change: Change,
    decisions: dict[str, Decision],
    matches: dict[int, Match],
    places: dict[str, str],
) -> dict:
    kind = editable_kind(change)
    photo = _photo(review, change)
    decision = decisions.get(change.id)
    edited = decision.value if decision and decision.status == EDITED else None
    return {
        "id": change.id,
        "label": field_label(change.field),
        "kind": change.kind,
        "his": _his_lines(review, change, matches),
        "his_place": _his_place_title(change, places),
        "mine": value_lines(change.mine),
        "proposed": _proposed_lines(change),
        "sensitive": change.sensitive,
        "informational": change.kind == MISSING_IN_HIS or change.waiting_on is not None,
        "waiting_on": change.waiting_on,
        "waiting_name": _waiting_name(review, change),
        "editable": kind,
        "edit_seed": edit_seed(change, kind) if kind else None,
        "edit_lines": edit_lines(kind, edited) if edited is not None else None,
        "decision": _decision_view(decision),
        "photo": photo,
        "photo_missing": change.field == "media" and photo is None,
        "mine_photos": _mine_photos(review, change),
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


def _mine_url(review: Review, mine: GrampsFullPerson) -> str | None:
    if not review.gramps_url:
        return None
    return f"{review.gramps_url.rstrip('/')}/person/{mine.gramps_id}"


def _his_url(review: Review, person: Person) -> str | None:
    return page_url(review.tree_name, person.address) if person.address else None


def _candidate_view(
    review: Review,
    mine: GrampsFullPerson,
    change: Change,
    decisions: dict[str, Decision],
) -> dict:
    return {
        "mine_url": _mine_url(review, mine),
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


def people_view(
    review: Review, decisions: dict[str, Decision], derived: Derived
) -> list[dict]:
    by_person: dict[int, list[Change]] = {}
    links: dict[int, list[Change]] = {}
    for change in derived.changes:
        target = links if is_link(change) else by_person
        target.setdefault(change.person, []).append(change)
    mine_by_handle = {p.handle: p for p in review.tree.people}
    places = place_mapping(review, decisions)
    refused = refused_people(decisions)
    result = []
    for key, person in sorted(review.people.items()):
        rows = [
            row_view(review, c, decisions, derived.matches, places)
            for c in by_person.get(key, [])
        ]
        match = derived.matches[key]
        new = any(c.kind == NEW_PERSON for c in by_person.get(key, []))
        actionable = [r for r in rows if not r["informational"]]
        found = mine_by_handle.get(match.handle)
        result.append(
            {
                "key": key,
                "name": display_name(person),
                "years": _his_years(person),
                "state": _state(rows, match.handle is not None, new),
                "his_url": _his_url(review, person),
                "mine": found
                and {
                    "id": found.gramps_id,
                    "name": _mine_name(found),
                    "url": _mine_url(review, found),
                },
                "why": match.why,
                "refused": key in refused,
                "changes": rows,
                "candidates": [
                    _candidate_view(
                        review, mine_by_handle[c.proposed["handle"]], c, decisions
                    )
                    for c in links.get(key, [])
                ],
                "photos_missing": sum(
                    1 for r in rows if r["photo_missing"] and not r["decision"]
                ),
                "actionable": len(actionable),
                "decided": sum(1 for r in actionable if r["decision"]),
            }
        )
    return result


def _accepted(
    changes: list[Change], decisions: dict[str, Decision]
) -> list[tuple[Change, Decision]]:
    return [
        (c, decisions[c.id])
        for c in changes
        if c.id in decisions and decisions[c.id].status in (ACCEPTED, EDITED)
    ]


def _verb(change: Change) -> str:
    if change.kind == NEW_PERSON:
        return "Create person"
    if is_link(change):
        return "Link to existing person"
    return "Add" if change.kind == NEW_VALUE else "Change"


def _preview_item(
    review: Review, change: Change, decision: Decision, matches: dict[int, Match]
) -> dict:
    edited = decision.status == EDITED
    return {
        "person": display_name(review.people[change.person]),
        "action": _verb(change),
        "label": field_label(change.field),
        "lines": edit_lines(editable_kind(change), decision.value)
        if edited
        else _his_lines(review, change, matches),
        "edited": edited,
        "sensitive": change.sensitive,
    }


def apply_command(pages: Path, data_dir: Path) -> str:
    return (
        f"geneanet-drift --data-dir {shlex.quote(str(data_dir))} "
        f"apply --pages {shlex.quote(str(pages))}"
    )


def preview_view(
    review: Review, decisions: dict[str, Decision], derived: Derived
) -> dict:
    items, skipped = [], []
    for change, decision in _accepted(derived.changes, decisions):
        item = _preview_item(review, change, decision, derived.matches)
        reason = skip_reason(change)
        if reason is None:
            items.append(item)
        else:
            skipped.append({**item, "reason": reason})
    return {
        "count": len(items) + len(skipped),
        "items": items,
        "skipped": skipped,
    }


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


PLACE_SUGGESTIONS = 3


def his_place_uses(review: Review) -> Counter[str]:
    return Counter(
        place
        for person in review.people.values()
        for place in (
            *(e.place for e in person.events),
            *(u.place for u in person.unions),
        )
        if place
    )


def _place_suggestions(review: Review, text: str) -> list[dict]:
    words = set(normalise(text).split())
    found = [p for p in review.tree.places if words <= set(normalise(p.title).split())]
    found.sort(key=lambda p: (len(p.title), p.title))
    return [{"handle": p.handle, "title": p.title} for p in found[:PLACE_SUGGESTIONS]]


def _place_mapping_view(titles: dict[str, str], decision: Decision | None):
    if decision is None or decision.value not in titles:
        return None
    return {"handle": decision.value, "title": titles[decision.value]}


def place_titles(review: Review) -> list[dict]:
    found = sorted(review.tree.places, key=lambda p: (p.title, p.handle))
    return [{"handle": p.handle, "title": p.title} for p in found]


def place_view(review: Review, decisions: dict[str, Decision]) -> list[dict]:
    titles = {p.handle: p.title for p in review.tree.places}
    return [
        {
            "text": text,
            "uses": uses,
            "mapping": _place_mapping_view(titles, decisions.get(PLACE_PREFIX + text)),
            "suggestions": _place_suggestions(review, text),
        }
        for text, uses in sorted(his_place_uses(review).items())
    ]
