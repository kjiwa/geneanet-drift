import copy
import json
import mimetypes
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from geneanet_drift import records
from geneanet_drift.capture import Person
from geneanet_drift.config import read_json, write_json_atomic
from geneanet_drift.decisions import ACCEPTED, EDITED, Decision
from geneanet_drift.diff import EVENT_KINDS, MISSING_IN_HIS, NEW_VALUE, Change
from geneanet_drift.feed import name_key, tree_url
from geneanet_drift.gedcom import media_path
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsError,
    GrampsEvent,
    GrampsTree,
    GrampsWriter,
    HttpError,
    References,
)
from geneanet_drift.match_tree import LINKED, Match, display_name
from geneanet_drift.review import source_title

PHASE_SETUP, PHASE_PLACES, PHASE_MEDIA, PHASE_PEOPLE, PHASE_EDITS = range(5)
OBJECTS = "/api/objects/"
HISTORY = "/api/transactions/history/"
ADD, EDIT = "AddObject", "EditObject"
INDEXED_EVENTS = ("birth", "death")
BACKUP_PATH = "/api/exporters/gramps/file"
GZIP_MAGIC = b"\x1f\x8b"
TASK_POLLS = 120
TASK_INTERVAL = 0.5
HISTORY_WINDOW = 50
UNDO_WINDOW = 500


class DriftError(GrampsError):
    pass


@dataclass(frozen=True)
class Patch:
    """Edits applied to the object as read just before its PUT; paths are dotted."""

    sets: tuple[tuple[str, object], ...] = ()
    appends: tuple[tuple[str, object], ...] = ()
    # (index field, list field): point the index at the last list item if unset.
    indexes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Upload:
    path: Path
    mime: str
    title: str
    person: str


@dataclass(frozen=True)
class Step:
    change_id: str
    index: int
    method: str
    path: str
    body: object
    requires: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Inputs:
    people: dict[int, Person]
    matches: dict[int, Match]
    tree: GrampsTree
    references: References
    tree_name: str
    today: date
    media_dir: Path


Row = tuple[int, str, str, str, object, tuple[str, ...], str]


def _chosen(changes: list[Change], decisions: dict[str, Decision]):
    for change in changes:
        decision = decisions.get(change.id)
        if decision and decision.status == ACCEPTED:
            yield change, change.proposed
        elif decision and decision.status == EDITED:
            yield change, decision.value


def _is_handle(ref: object) -> bool:
    return isinstance(ref, dict) and "handle" in ref


class _Planner:
    def __init__(self, inputs: Inputs):
        self.inputs = inputs
        self.mine = {p.handle: p for p in inputs.tree.people}
        self.families = {f.handle: f for f in inputs.tree.families}
        self.source = inputs.references.sources.get(source_title(inputs.tree_name))
        self.source = self.source or records.handle_for(
            "source", source_title(inputs.tree_name)
        )
        self.cited = False
        self.rows: list[Row] = []
        self.skipped: list[str] = []
        self.new_handles: dict[int, str] = {}
        self.rank: dict[str, int] = {}
        self.sex: dict[str, str] = {}
        self.linked: dict[int, str] = {}  # his key -> handle, by accepted link
        self.new_places: dict[str, tuple[str, str, str | None]] = {}
        self.planned: dict[str, list[str]] = {}  # new family -> children
        self.partners: dict[str, list[str]] = {}  # new family -> partners

    def row(self, phase, change, method, path, body, requires, summary) -> None:
        self.rows.append((phase, change.id, method, path, body, requires, summary))

    def skip(self, change: Change, reason: str) -> None:
        self.skipped.append(f"{change.field} of {self.label(change)}: {reason}")

    def label(self, change: Change) -> str:
        return display_name(self.inputs.people[change.person])

    def handle_of(self, change: Change) -> str | None:
        match = self.inputs.matches.get(change.person)
        return match.handle if match and match.kind == LINKED else None

    def resolve(self, ref) -> str | None:
        if _is_handle(ref):
            return ref["handle"]
        if not ref:
            return None
        return self.key_handle(ref["key"])

    def key_handle(self, key: int | None) -> str | None:
        match = self.inputs.matches.get(key)
        if match and match.kind == LINKED:
            return match.handle
        return self.new_handles.get(key) or self.linked.get(key)

    def gender(self, handle: str) -> str:
        if handle in self.mine:
            return self.mine[handle].gender
        return self.sex.get(handle, "unknown")

    def place(self, title: str) -> str:
        parts = [p.strip() for p in title.split(",") if p.strip()]
        handle = None
        for start in range(len(parts) - 1, -1, -1):
            key = name_key(", ".join(parts[start:]))
            known = self.inputs.references.places.get(key)
            known = known or (self.new_places.get(key) or (None,))[0]
            if known is None:
                known = records.handle_for("place", key)
                self.new_places[key] = (known, parts[start], handle)
            handle = known
        return handle or ""

    def url(self, change: Change) -> str:
        person = self.inputs.people[change.person]
        if person.address is None:
            return f"{tree_url(self.inputs.tree_name)}?{urlencode({'lang': 'en', 'i': person.key})}"
        query = {
            "n": person.address.surname,
            "p": person.address.given,
            "oc": person.address.occurrence,
        }
        return f"{tree_url(self.inputs.tree_name)}?{urlencode(query)}"

    def citation(self, change: Change, label: str) -> dict:
        self.cited = True
        return records.citation(
            records.handle_for("citation", change.id, label),
            self.source,
            self.label(change),
            self.url(change),
            self.inputs.today,
        )

    def evidence(self, change: Change, label: str, kind: int):
        """The citation and rationale note recording where a written fact came from."""
        cite = self.citation(change, label)
        why = records.handle_for("note", change.id, label)
        text = (
            f"Applied from Geneanet on {self.inputs.today.isoformat()}: "
            f"{change.field} ({change.kind}). "
            f"Geneanet: {json.dumps(change.his, ensure_ascii=False)}. "
            f"Gramps before: {json.dumps(change.mine, ensure_ascii=False)}."
        )
        return [cite, records.note(why, text, kind)], cite["handle"], why

    def edit(self, change, endpoint, handle, summary, patch, objects, evidence_kind):
        """Creates the evidence and extras, then patches the target to point at them."""
        evidence, cite, why = self.evidence(change, endpoint, evidence_kind)
        appends = (*patch.appends, ("citation_list", cite), ("note_list", why))
        self.row(
            PHASE_EDITS,
            change,
            "POST",
            OBJECTS,
            [*objects, *evidence],
            (ADD,),
            f"add the citation and note for: {summary}",
        )
        patched = Patch(patch.sets, appends, patch.indexes)
        self.row(
            PHASE_EDITS,
            change,
            "PUT",
            f"/api/{endpoint}/{handle}",
            patched,
            (EDIT,),
            summary,
        )

    def patch_row(self, change, endpoint, handle, patch, summary) -> None:
        path = f"/api/{endpoint}/{handle}"
        self.row(PHASE_EDITS, change, "PUT", path, patch, (EDIT,), summary)

    def plan(self, change: Change, value) -> None:
        field = change.field
        if change.kind == MISSING_IN_HIS:
            self.skip(change, "only in Gramps; nothing is deleted")
        elif field == "person":
            self.new_person(change, value)
        elif field.startswith("link:"):
            self.link(change, value)
        elif self.handle_of(change) is None:
            self.skip(change, "his person is not linked to a Gramps person")
        elif field.startswith("event:"):
            self.event(change, value)
        elif field.startswith("parent:"):
            self.parent(change, value)
        elif field.startswith("union:"):
            self.union(change, value)
        elif field in SIMPLE_FIELDS:
            SIMPLE_FIELDS[field](self, change, value)
        else:
            self.skip(change, "no write is defined for this field")

    # People

    def new_person(self, change: Change, bundle: dict) -> None:
        handle = self.new_handles[change.person]
        objects, cite, why = self.evidence(change, "person", records.NOTE_PERSON)
        refs, indexes = self.new_events(change, bundle, objects)
        notes = self.new_notes(change, bundle, objects)
        if bundle["media"]:
            self.skip(change, "media of a new person waits until he is linked")
        names = [records.alternate_name(n) for n in bundle["other_names"]]
        if None in names:
            self.skip(change, "a blank other name was left out")
        names = [n for n in names if n]
        parents = [h for h in map(self.resolve, bundle["parents"]) if h]
        families = self.person_families(change, handle, parents, bundle["unions"])
        objects.append(
            records.person(
                handle,
                bundle["given"],
                bundle["surname"],
                bundle["gender"],
                alternate_names=names,
                event_ref_list=refs,
                citation_list=[cite],
                note_list=[why, *notes],
                **indexes,
            )
        )
        objects.extend(families)
        requires = (ADD, EDIT) if families else (ADD,)
        summary = f"create {self.label(change)}"
        self.row(PHASE_PEOPLE, change, "POST", OBJECTS, objects, requires, summary)

    def new_events(self, change, bundle, objects):
        refs: list[dict] = []
        indexes: dict[str, int] = {}
        facts = {k: v for k, v in bundle["events"].items()}
        if bundle["occupation"]:
            facts["occupation"] = {"date": None, "place": None}
        for kind, fact in facts.items():
            kind = EVENT_KINDS.get(kind, kind)
            handle = records.handle_for("event", change.id, kind)
            place = self.place(fact["place"]) if fact["place"] else None
            cite = self.citation(change, kind)
            objects.extend(
                [
                    cite,
                    records.event(
                        handle,
                        kind,
                        fact["date"],
                        place,
                        bundle["occupation"] if kind == "occupation" else "",
                        citations=[cite["handle"]],
                    ),
                ]
            )
            if kind in INDEXED_EVENTS:
                indexes[f"{kind}_ref_index"] = len(refs)
            refs.append(records.event_ref(handle))
        return refs, indexes

    def new_notes(self, change, bundle, objects) -> list[str]:
        texts = [n["text"] for n in bundle["notes"]]
        texts += [f"{s['fact']}: {s['text']}" for s in bundle["sources"]]
        handles = []
        for number, text in enumerate(texts):
            handle = records.handle_for("note", change.id, f"bundle-{number}")
            objects.append(records.note(handle, text, records.NOTE_GENERAL))
            handles.append(handle)
        return handles

    def person_families(self, change, handle, parents, unions) -> list[dict]:
        wanted = [(parents, [handle], None)] if parents else []
        for union in unions:
            spouse = self.resolve(union["spouse"])
            children = [h for h in map(self.resolve, union["children"]) if h]
            marriage = {"date": union["date"], "place": union["place"]}
            wanted.append(([handle, *([spouse] if spouse else [])], children, marriage))
        objects: list[dict] = []
        for partners, children, marriage in wanted:
            if self.is_last_new(handle, [*partners, *children]):
                objects.extend(self.family(change, partners, children, marriage))
        return objects

    def is_last_new(self, handle: str, participants: list[str]) -> bool:
        ranks = [self.rank[h] for h in participants if h in self.rank]
        return self.rank[handle] == max(ranks)

    def existing_family(self, partners: list[str]):
        wanted = set(partners)
        return next(
            (
                f
                for f in self.families.values()
                if {f.father, f.mother} - {None} == wanted
            ),
            None,
        )

    def family(self, change, partners, children, marriage, evidence=False):
        """Objects for a new family, or rows adding children to one that exists."""
        key = records.handle_for("family", *sorted(partners))
        known = self.existing_family(partners)
        if known is not None or key in self.planned:
            self.join_family(change, known.handle if known else key, children)
            return []
        self.planned[key] = list(children)
        self.partners[key] = list(partners)
        father, mother = self.order(partners)
        objects: list[dict] = []
        refs: list[dict] = []
        if marriage and (marriage["date"] or marriage["place"]):
            handle = records.handle_for("event", change.id, key)
            place = self.place(marriage["place"]) if marriage["place"] else None
            objects.append(
                records.event(handle, "marriage", marriage["date"], place, "")
            )
            refs.append(records.event_ref(handle, family=True))
        extras: dict = {}
        if evidence:
            more, cite, why = self.evidence(change, key, records.NOTE_FAMILY)
            objects.extend(more)
            extras = {"citation_list": [cite], "note_list": [why]}
        objects.append(records.family(key, father, mother, children, refs, **extras))
        return objects

    def order(self, partners: list[str]) -> tuple[str | None, str | None]:
        fathers = [p for p in partners if self.gender(p) == "male"]
        if not fathers and len(partners) == 1 and self.gender(partners[0]) == "female":
            return None, partners[0]
        father = (fathers or partners)[0]
        return father, next((p for p in partners if p != father), None)

    def join_family(self, change, family_handle, children) -> None:
        """Adds children whether or not they are already in the live family, so
        the steps do not depend on what an interrupted run already wrote."""
        present = set(self.planned.get(family_handle, ()))
        added = [c for c in children if c not in present]
        for child in added:
            self.patch_row(
                change,
                "families",
                family_handle,
                Patch(appends=(("child_ref_list", records.child_ref(child)),)),
                f"add a child to family {family_handle}",
            )
            self.patch_row(
                change,
                "people",
                child,
                Patch(appends=(("parent_family_list", family_handle),)),
                f"link {child} to family {family_handle}",
            )
        if family_handle in self.planned:
            self.planned[family_handle] += added

    # Existing people

    def link(self, change: Change, value: dict) -> None:
        summary = f"cite his page on Gramps person {value['handle']}"
        self.edit(
            change,
            "people",
            value["handle"],
            summary,
            Patch(),
            [],
            records.NOTE_PERSON,
        )

    def simple(self, change, summary, patch, objects=()):
        handle = self.handle_of(change)
        self.edit(
            change,
            "people",
            handle,
            f"{summary} for {self.label(change)}",
            patch,
            list(objects),
            records.NOTE_PERSON,
        )

    def gender_field(self, change, value) -> None:
        patch = Patch(sets=(("gender", records.GENDER_VALUES[value]),))
        self.simple(change, f"set gender {value}", patch)

    def given_field(self, change, value) -> None:
        patch = Patch(sets=(("primary_name.first_name", value),))
        self.simple(change, f"set given name {value}", patch)

    def surname_field(self, change, value) -> None:
        patch = Patch(sets=(("primary_name.surname_list.0.surname", value),))
        self.simple(change, f"set surname {value}", patch)

    def other_name_field(self, change, value) -> None:
        name = records.alternate_name(value)
        if name is None:
            self.skip(change, "the other name is blank")
            return
        patch = Patch(appends=(("alternate_names", name),))
        self.simple(change, f"add the other name {value}", patch)

    def note_field(self, change, value) -> None:
        self.add_note(change, value["text"], f"add a {value['kind']} note")

    def source_field(self, change, value) -> None:
        text = f"{value['fact']}: {value['text']}"
        self.add_note(change, text, "add his source text")

    def add_note(self, change, text, summary) -> None:
        handle = records.handle_for("note", change.id, "body")
        note = records.note(handle, text, records.NOTE_GENERAL)
        patch = Patch(appends=(("note_list", handle),))
        self.simple(change, summary, patch, [note])

    def occupation_field(self, change, value) -> None:
        person = self.mine[self.handle_of(change)]
        existing = next(
            (e for e in person.events if e.kind.casefold() == "occupation"), None
        )
        self.event_fact(change, "occupation", existing, {"description": value})

    def media_field(self, change, value) -> None:
        person = self.inputs.people[change.person]
        media = next(m for m in person.media if m.id == value["id"])
        path = self.inputs.media_dir / media_path(person.key, media)
        if not path.is_file():
            self.skip(change, f"{path.name} was not saved from his page")
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        handle = self.handle_of(change)
        upload = Upload(path, mime, value["title"], handle)
        self.row(
            PHASE_MEDIA,
            change,
            "POST",
            "/api/media/",
            upload,
            (ADD, EDIT),
            f"upload {path.name} ({mime}) to {self.label(change)}",
        )
        self.edit(
            change,
            "people",
            handle,
            f"cite the picture {value['title']!r}",
            Patch(),
            [],
            records.NOTE_PERSON,
        )

    # Events

    def event(self, change: Change, value: dict) -> None:
        kind = EVENT_KINDS[change.field.removeprefix("event:")]
        person = self.mine[self.handle_of(change)]
        existing = next((e for e in person.events if e.kind.casefold() == kind), None)
        known = change.mine or {}
        fields = {
            "date": value["date"] if value["date"] != known.get("date") else None,
            "place": value["place"] if value["place"] != known.get("place") else None,
        }
        if not any(fields.values()):
            self.skip(change, "nothing to set; Gramps already holds these values")
            return
        self.event_fact(change, kind, existing, fields)

    def event_fact(self, change, kind, existing: GrampsEvent | None, fields) -> None:
        wanted = {k: v for k, v in fields.items() if v}
        if existing is None:
            self.new_event(change, kind, wanted)
            return
        sets = []
        if "date" in wanted:
            sets.append(("date", records.date_json(wanted["date"])))
        if "place" in wanted:
            sets.append(("place", self.place(wanted["place"])))
        if "description" in wanted:
            sets.append(("description", wanted["description"]))
        summary = f"update the {kind} of {self.label(change)}"
        self.edit(
            change,
            "events",
            existing.handle,
            summary,
            Patch(tuple(sets)),
            [],
            records.NOTE_EVENT,
        )

    def new_event(self, change, kind, wanted) -> None:
        handle = records.handle_for("event", change.id, kind)
        place = self.place(wanted["place"]) if "place" in wanted else None
        evidence, cite, why = self.evidence(change, kind, records.NOTE_EVENT)
        event = records.event(
            handle,
            kind,
            wanted.get("date"),
            place,
            wanted.get("description", ""),
            [cite],
            [why],
        )
        summary = f"add a {kind} to {self.label(change)}"
        self.row(
            PHASE_EDITS, change, "POST", OBJECTS, [event, *evidence], (ADD,), summary
        )
        indexes = (
            ((f"{kind}_ref_index", "event_ref_list"),) if kind in INDEXED_EVENTS else ()
        )
        patch = Patch(
            appends=(("event_ref_list", records.event_ref(handle)),), indexes=indexes
        )
        self.patch_row(
            change, "people", self.handle_of(change), patch, f"attach the {kind}"
        )

    # Relatives

    def parent(self, change: Change, value) -> None:
        parent = self.resolve(value)
        child = self.handle_of(change)
        if parent is None:
            self.skip(change, "the parent is not in Gramps or in this plan")
            return
        label = self.label(change)
        evidence, cite, why = self.evidence(change, "parent", records.NOTE_FAMILY)
        self.row(
            PHASE_EDITS,
            change,
            "POST",
            OBJECTS,
            evidence,
            (ADD,),
            f"add the citation and note for: record the parent of {label}",
        )
        found = self.family_of(parent, child, self.other_parents(change, parent))
        family = self.parent_family(change, parent, child, found, (cite, why))
        self.patch_row(
            change,
            "people",
            child,
            Patch(appends=(("parent_family_list", family),)),
            f"link {label} to family {family}",
        )

    def parent_family(self, change, parent, child, found, evidence) -> str:
        """One step that creates the family or joins the child to a found one."""
        cite, why = evidence
        label = self.label(change)
        if found is None:
            key = records.handle_for("family", parent)
            father, mother = self.order([parent])
            family = records.family(
                key, father, mother, [child], [], citation_list=[cite], note_list=[why]
            )
            self.planned[key] = [child]
            self.partners[key] = [parent]
            self.row(
                PHASE_EDITS,
                change,
                "POST",
                OBJECTS,
                [family],
                (ADD, EDIT),
                f"create a family for {label} with parent {parent}",
            )
            return key
        handle, slot = found
        sets = ((slot, parent),) if slot else ()
        appends = (
            ("child_ref_list", records.child_ref(child)),
            ("citation_list", cite),
            ("note_list", why),
        )
        self.patch_row(
            change,
            "families",
            handle,
            Patch(sets, appends),
            f"add {label} to family {handle}",
        )
        if handle in self.planned:
            self.partners[handle] += [parent] if slot else []
            self.planned[handle] += [child] if child not in self.planned[handle] else []
        return handle

    def other_parents(self, change: Change, parent: str) -> set[str]:
        refs = self.inputs.people[change.person].parents
        found = {self.key_handle(r.key) for r in refs if r.key is not None}
        return found - {parent, None}

    def family_candidates(self):
        for handle, family in self.families.items():
            yield handle, family.father, family.mother, list(family.children)
        for handle, children in self.planned.items():
            father, mother = self.order(self.partners[handle])
            yield handle, father, mother, children

    def family_of(self, parent: str, child: str, others: set[str]):
        """(family, free slot) for the parent: a family they partner with the
        child's other parent, else the child's family missing this parent."""
        best, best_score = None, 0
        for handle, father, mother, children in self.family_candidates():
            partners = [p for p in (father, mother) if p]
            if parent in partners:
                spouse = [p for p in partners if p != parent]
                score = 3 if spouse and spouse[0] in others else 0 if spouse else 1
                found = (handle, None)
            elif child in children and len(partners) == 1 and partners[0] in others:
                score = 2
                found = (handle, "father_handle" if father is None else "mother_handle")
            else:
                continue
            if score > best_score:
                best, best_score = found, score
        return best

    def his_children(self, change: Change) -> list[str]:
        key = int(change.field.removeprefix("union:"))
        person = self.inputs.people[change.person]
        union = next(u for u in person.unions if u.key == key)
        handles = (self.key_handle(c.key) for c in union.children)
        return [h for h in handles if h]

    def union(self, change: Change, value: dict) -> None:
        person = self.handle_of(change)
        spouse = self.resolve(value["spouse"])
        partners = [person, *([spouse] if spouse else [])]
        if change.kind == NEW_VALUE:
            children = [h for h in map(self.resolve, value["children"]) if h]
            objects = self.family(change, partners, children, value, evidence=True)
            if objects:
                summary = f"create a family for {self.label(change)}"
                requires = (ADD, EDIT)
                self.row(
                    PHASE_EDITS, change, "POST", OBJECTS, objects, requires, summary
                )
            else:
                self.skip(change, "the family is already planned or exists")
            return
        family = self.existing_family(partners)
        if family is None:
            self.skip(change, "the family to change was not found")
            return
        children = self.his_children(change)
        refs = tuple(("child_ref_list", records.child_ref(c)) for c in children)
        self.edit(
            change,
            "families",
            family.handle,
            f"update the family of {self.label(change)}",
            Patch(appends=refs),
            [],
            records.NOTE_FAMILY,
        )
        for child in children:
            self.patch_row(
                change,
                "people",
                child,
                Patch(appends=(("parent_family_list", family.handle),)),
                f"link {child} to family {family.handle}",
            )

    # Result

    def setup_rows(self) -> list[Row]:
        title = source_title(self.inputs.tree_name)
        if not self.cited or self.inputs.references.sources.get(title):
            return []
        repository = records.handle_for("repository", "Geneanet")
        objects = [
            records.repository(repository, "Geneanet"),
            records.source(self.source, title, repository),
        ]
        return [
            (
                PHASE_SETUP,
                f"source-{self.source}",
                "POST",
                OBJECTS,
                objects,
                (ADD,),
                f"create the repository Geneanet and the source {title!r}",
            )
        ]

    def place_rows(self) -> list[Row]:
        if not self.new_places:
            return []
        objects = [
            records.place(handle, label, parent)
            for handle, label, parent in self.new_places.values()
        ]
        names = ", ".join(label for _, label, _ in self.new_places.values())
        ident = records.handle_for("places", *(o["handle"] for o in objects))
        return [
            (
                PHASE_PLACES,
                f"places-{ident[2:14]}",
                "POST",
                OBJECTS,
                objects,
                (ADD,),
                f"create places: {names}",
            )
        ]

    def finish(self) -> Plan:
        rows = sorted(
            [*self.setup_rows(), *self.place_rows(), *self.rows],
            key=lambda row: row[0],
        )
        seen: dict[str, int] = {}
        steps = []
        for _, change_id, method, path, body, requires, summary in rows:
            index = seen.get(change_id, 0)
            seen[change_id] = index + 1
            steps.append(Step(change_id, index, method, path, body, requires, summary))
        return Plan(tuple(steps), tuple(self.skipped))


SIMPLE_FIELDS: dict[str, Callable] = {
    "gender": _Planner.gender_field,
    "given": _Planner.given_field,
    "surname": _Planner.surname_field,
    "other_name": _Planner.other_name_field,
    "note": _Planner.note_field,
    "source": _Planner.source_field,
    "occupation": _Planner.occupation_field,
    "media": _Planner.media_field,
}


def build_plan(
    changes: list[Change], decisions: dict[str, Decision], inputs: Inputs
) -> Plan:
    planner = _Planner(inputs)
    chosen = list(_chosen(changes, decisions))
    for change, bundle in chosen:
        if change.field == "person":
            handle = records.handle_for("person", change.id)
            planner.new_handles[change.person] = handle
            planner.rank[handle] = len(planner.rank)
            planner.sex[handle] = bundle["gender"]
        elif change.field.startswith("link:"):
            planner.linked[change.person] = bundle["handle"]
    for change, value in chosen:
        planner.plan(change, value)
    return planner.finish()


def preview(plan: Plan, logged: set[tuple[str, int]] = frozenset()) -> str:
    done = sum((s.change_id, s.index) in logged for s in plan.steps)
    lines = [f"{len(plan.steps)} steps, {len(plan.steps) - done} to write"]
    for number, step in enumerate(plan.steps, start=1):
        mark = "done" if (step.change_id, step.index) in logged else "    "
        lines.append(f"{number:>3}. {mark} {step.summary}")
        lines.append(f"     {step.method} {step.path}  change {step.change_id}")
    for reason in plan.skipped:
        lines.append(f"skipped: {reason}")
    return "\n".join(lines)


# Execution


def read_log(path: Path) -> list[dict]:
    return read_json(path, {"steps": []})["steps"]


def _write_log(path: Path, log: list[dict]) -> None:
    write_json_atomic(path, {"steps": log})


def _entry(step: Step) -> dict:
    return {
        "change": step.change_id,
        "index": step.index,
        "summary": step.summary,
        "created": [],
        "updated": [],
        "transactions": [],
    }


def _object_path(item: dict) -> str:
    return f"/api/{records.ENDPOINTS[item['_class']]}/{item['handle']}"


def _record_changes(entry: dict, changes: list[dict]) -> None:
    for item in changes:
        bucket = "created" if item.get("type") == "add" else "updated"
        path = _object_path(item)
        if path not in entry[bucket]:
            entry[bucket].append(path)


def _parts(path: str) -> list[str | int]:
    return [int(p) if p.isdigit() else p for p in path.split(".")]


def _walk(obj, parts):
    for part in parts:
        obj = obj[part]
    return obj


def _contains(actual, expected) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return "_class" in expected
        return all(
            _contains(actual.get(k), v) for k, v in expected.items() if k != "_class"
        )
    return actual == expected


def _has(items: list, wanted) -> bool:
    return any(_contains(item, wanted) for item in items)


def patched(obj: dict, patch: Patch) -> dict:
    for path, value in patch.sets:
        *head, last = _parts(path)
        _walk(obj, head)[last] = value
    for path, value in patch.appends:
        items = _walk(obj, _parts(path))
        if not _has(items, value):
            items.append(value)
    for index_field, list_field in patch.indexes:
        if obj.get(index_field, -1) == -1:
            obj[index_field] = len(obj[list_field]) - 1
    return obj


def holds(obj: dict, patch: Patch) -> bool:
    return all(_contains(_walk(obj, _parts(p)), v) for p, v in patch.sets) and all(
        _has(_walk(obj, _parts(p)), v) for p, v in patch.appends
    )


def _author(transaction: dict) -> str | None:
    return transaction.get("connection", {}).get("user", {}).get("name")


class _Journal:
    """Reads the transaction ids a write produced; the API's reply carries none.

    A transaction counts if it is newer than the newest one seen before the
    write and was made by the write user. Another session of the same user
    writing at the same moment would be misattributed.
    """

    def __init__(self, writer: GrampsWriter):
        self._writer = writer
        self._last = self._newest()

    def _newest(self) -> int:
        batch, _ = self._writer.get(HISTORY, sort="-id", pagesize=1)
        return batch[0]["id"] if batch else 0

    def record(self, entry: dict) -> None:
        batch, _ = self._writer.get(HISTORY, sort="-id", pagesize=HISTORY_WINDOW)
        fresh = sorted(
            t["id"]
            for t in batch
            if t["id"] > self._last and _author(t) == self._writer.username
        )
        entry["transactions"].extend(fresh)
        self._last = max([self._last, *fresh])


def _put(writer, journal, entry, path, patch: Patch) -> None:
    current, _ = writer.get(path)
    new = patched(copy.deepcopy(current), patch)
    if new == current:
        return
    changes = writer.put_json(path, new)
    _record_changes(entry, [{**c, "type": "update"} for c in changes])
    journal.record(entry)


def _run_objects(writer, journal, entry, step: Step) -> None:
    _record_changes(entry, writer.post_json(step.path, step.body))
    journal.record(entry)


def _run_put(writer, journal, entry, step: Step) -> None:
    _put(writer, journal, entry, step.path, step.body)


def _run_upload(writer, journal, entry, step: Step) -> None:
    upload: Upload = step.body
    created = writer.post_file(step.path, upload.path.read_bytes(), upload.mime)
    _record_changes(entry, created)
    journal.record(entry)
    media = created[0]["handle"]
    _put(
        writer, journal, entry, f"/api/media/{media}", Patch((("desc", upload.title),))
    )
    attach = Patch(appends=(("media_list", records.media_ref(media)),))
    _put(writer, journal, entry, f"/api/people/{upload.person}", attach)


def _runner(step: Step):
    if isinstance(step.body, Upload):
        return _run_upload
    return _run_put if isinstance(step.body, Patch) else _run_objects


_DRIFT_ADVICE = (
    "Check the object in Gramps. To keep its current state and write the step "
    "again, delete that step's entry from applied.json and rerun; to roll back, "
    "run `geneanet-drift undo --last N`."
)


def _verify(writer: GrampsWriter, step: Step, entry: dict) -> None:
    if entry.get("partial"):
        raise DriftError(
            f"step {step.change_id}:{step.index} ({step.summary}) was interrupted; "
            "run `geneanet-drift undo --last 1`, then apply again"
        )
    try:
        for path in entry["created"]:
            writer.get(path)
        if isinstance(step.body, Patch):
            current, _ = writer.get(step.path)
            if not holds(current, step.body):
                raise DriftError(
                    f"{step.path} no longer holds what step "
                    f"{step.change_id}:{step.index} wrote ({step.summary}); it "
                    f"changed since. {_DRIFT_ADVICE}"
                )
    except DriftError:
        raise
    except GrampsError as error:
        raise DriftError(
            f"step {step.change_id}:{step.index} ({step.summary}) cannot be "
            f"re-read: {error}. {_DRIFT_ADVICE}"
        ) from error


def apply(
    plan: Plan,
    writer: GrampsWriter,
    log_path: Path,
    say: Callable[[str], None] = lambda message: None,
) -> int:
    """Runs the steps not yet logged, stopping at the first error or drifted step."""
    log = read_log(log_path)
    done = {(e["change"], e["index"]): e for e in log}
    pending = []
    for step in plan.steps:
        entry = done.get((step.change_id, step.index))
        if entry is None:
            pending.append(step)
        else:
            _verify(writer, step, entry)
    journal = _Journal(writer)
    for step in pending:
        missing = set(step.requires) - writer.permissions
        if missing:
            raise GrampsError(f"the write user lacks {', '.join(sorted(missing))}")
    for number, step in enumerate(pending, start=1):
        entry = _entry(step)
        try:
            _runner(step)(writer, journal, entry, step)
        except Exception:
            if entry["created"] or entry["updated"]:
                log.append({**entry, "partial": True})
                _write_log(log_path, log)
            raise
        log.append(entry)
        _write_log(log_path, log)
        say(f"{number}/{len(pending)} {step.summary}")
    return len(pending)


def _wait_for(writer: GrampsWriter, task: str) -> None:
    for _ in range(TASK_POLLS):
        status, _ = writer.get(f"/api/tasks/{task}")
        if status["state"] == "SUCCESS":
            return
        if status["state"] == "FAILURE":
            raise GrampsError(f"task {task} failed")
        time.sleep(TASK_INTERVAL)
    raise GrampsError(f"task {task} did not finish")


def newest_steps(log: list[dict], last: int) -> list[dict]:
    return log[-last:] if last > 0 else []


def _already_undone(writer: GrampsWriter, transaction: int) -> bool:
    """True when every object the transaction added is gone already."""
    batch, _ = writer.get(HISTORY, sort="-id", pagesize=UNDO_WINDOW)
    row = next((t for t in batch if t["id"] == transaction), {})
    changes = [
        c for c in row.get("changes", []) if c.get("obj_class") in records.ENDPOINTS
    ]
    if not changes or any(c.get("trans_type") != 0 for c in changes):
        return False
    for change in changes:
        path = f"/api/{records.ENDPOINTS[change['obj_class']]}/{change['obj_handle']}"
        try:
            writer.get(path)
        except HttpError as error:
            if error.status != 404:
                raise
        else:
            return False
    return True


def _undo_transaction(writer: GrampsWriter, transaction: int) -> None:
    try:
        reply = writer.post_json(f"{HISTORY}{transaction}/undo", {})
        _wait_for(writer, reply["task"]["id"])
    except GrampsError:
        if not _already_undone(writer, transaction):
            raise


def undo(
    writer: GrampsWriter,
    log_path: Path,
    last: int,
    say: Callable[[str], None] = lambda message: None,
) -> int:
    """Reverts the newest logged steps, newest transaction first, saving the
    log after each transaction so an interrupted undo resumes where it stopped."""
    log = read_log(log_path)
    undone = 0
    for entry in reversed(newest_steps(log, last)):
        while entry["transactions"]:
            _undo_transaction(writer, entry["transactions"][-1])
            entry["transactions"].pop()
            _write_log(log_path, log)
        log.remove(entry)
        _write_log(log_path, log)
        undone += 1
        say(f"undid {entry['summary']}")
    return undone


def backup(reader: GrampsClient, directory: Path, now: datetime) -> Path:
    """Saves the whole tree as Gramps XML; any failure aborts the apply."""
    try:
        data = reader.fetch(BACKUP_PATH)
    except GrampsError as error:
        raise GrampsError(f"backup failed, nothing was written: {error}") from error
    if not data.startswith(GZIP_MAGIC):
        raise GrampsError("backup failed, nothing was written: not a Gramps export")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"gramps-{now.strftime('%Y%m%dT%H%M%S')}.gramps"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return path
