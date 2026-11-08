import copy
import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from geneanet_drift import records
from geneanet_drift.apply import (
    HISTORY,
    DriftError,
    Inputs,
    Patch,
    Upload,
    apply,
    backup,
    build_plan,
    holds,
    newest_steps,
    patched,
    preview,
    read_log,
    undo,
)
from geneanet_drift.capture import Media
from geneanet_drift.config import load_config, write_json
from geneanet_drift.decisions import ACCEPTED, EDITED, REJECTED, Decision
from geneanet_drift.diff import diff_people
from geneanet_drift.feed import name_key
from geneanet_drift.gedcom import media_path
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsError,
    GrampsWriter,
    References,
)
from geneanet_drift.match_tree import match_people
from tests.builders import (
    SOURCE,
    crosswalk,
    family,
    gramps,
    gramps_event,
    his,
    his_event,
    his_people,
    his_union,
    ref,
    tree,
)
from tests.conftest import BASE_URL, make_token

EDITOR = (
    "AddObject",
    "EditObject",
    "DeleteObject",
    "ViewPrivate",
    "EditOwnUser",
    "EditCustomFilter",
    "EditNameGroup",
)
BRINDLE = "Brindle Hollow, Marrow County, Eastmere"
TODAY = date(2026, 11, 8)
ENDPOINT_CLASSES = {v: k for k, v in records.ENDPOINTS.items()}


class FakeServer:
    """An in-memory Gramps Web that records every request it receives."""

    def __init__(self, mine=None, permissions=EDITOR):
        self.token = make_token(*permissions)
        self.store: dict[str, dict] = {"/api/metadata/": {}}
        self.requests: list[tuple[str, str]] = []
        self.history: list[dict] = []
        self.fail_on: tuple[str, str] | None = None
        self.fail_skip = 0
        self.media = 0
        self.undone: set[int] = set()
        for person in mine.people if mine else ():
            self.store[f"/api/people/{person.handle}"] = {
                "handle": person.handle,
                "citation_list": [],
                "note_list": [],
                "event_ref_list": [],
                "media_list": [],
                "parent_family_list": [],
            }
            for event in person.events:
                self.store[f"/api/events/{event.handle}"] = {
                    "handle": event.handle,
                    "date": {"dateval": [0, 0, event.date.year, False], "modifier": 0},
                    "citation_list": [],
                    "note_list": [],
                }
        for fam in mine.families if mine else ():
            self.store[f"/api/families/{fam.handle}"] = {
                "handle": fam.handle,
                "father_handle": fam.father,
                "mother_handle": fam.mother,
                "child_ref_list": [{"ref": c} for c in fam.children],
                "citation_list": [],
                "note_list": [],
            }

    @property
    def transactions(self) -> list[int]:
        return [t["id"] for t in self.history]

    def writes(self) -> list[tuple[str, str]]:
        return [
            r for r in self.requests if r[0] in ("POST", "PUT") and "token" not in r[1]
        ]

    def commit(self, user="editor", added=()) -> int:
        number = len(self.history) + 1
        changes = [{"obj_class": c, "obj_handle": h, "trans_type": 0} for c, h in added]
        self.history.append(
            {
                "id": number,
                "connection": {"user": {"name": user}},
                "changes": changes,
            }
        )
        return number

    def _add(self, item: dict) -> dict:
        cls = item["_class"]
        self.store[f"/api/{records.ENDPOINTS[cls]}/{item['handle']}"] = copy.deepcopy(
            item
        )
        return {"type": "add", "_class": cls, "handle": item["handle"]}

    def _post(self, path: str, body: bytes, headers):
        if path == "/api/objects/":
            changes = [self._add(item) for item in json.loads(body)]
            self.commit(added=[(c["_class"], c["handle"]) for c in changes])
            return 201, {}, json.dumps(changes).encode()
        if path == "/api/media/":
            self.media += 1
            handle = f"media{self.media}"
            change = self._add({"_class": "Media", "handle": handle, "desc": ""})
            self.store[f"/api/media/{handle}"]["mime"] = headers["Content-Type"]
            self.commit(added=[("Media", handle)])
            return 201, {}, json.dumps([change]).encode()
        if path.endswith("/undo"):
            return self._undo(int(path.split("/")[-2]))
        return 404, {}, b"{}"

    def _undo(self, number: int):
        self.requests.append(("UNDO", path_of(number)))
        fresh = number not in self.undone
        self.undone.add(number)
        for change in self.history[number - 1]["changes"] if fresh else ():
            key = (
                f"/api/{records.ENDPOINTS[change['obj_class']]}/{change['obj_handle']}"
            )
            self.store.pop(key, None)
        task = f"ok{number}" if fresh else f"fail{number}"
        return 202, {}, json.dumps({"task": {"id": task}}).encode()

    def _put(self, path: str, body: bytes):
        if path not in self.store:
            return 404, {}, b"{}"
        self.store[path] = json.loads(body)
        self.commit()
        cls = ENDPOINT_CLASSES[path.split("/")[2]]
        change = {"_class": cls, "handle": path.rsplit("/", 1)[1]}
        return 200, {}, json.dumps([change]).encode()

    def _get(self, path: str, query: dict):
        if path == HISTORY:
            size = int(query["pagesize"][0])
            rows = list(reversed(self.history))[:size]
            return 200, {}, json.dumps(rows).encode()
        if path.startswith("/api/tasks/"):
            failed = path.rsplit("/", 1)[1].startswith("fail")
            state = "FAILURE" if failed else "SUCCESS"
            return 200, {}, json.dumps({"state": state}).encode()
        if path in self.store:
            return 200, {}, json.dumps(self.store[path]).encode()
        return 404, {}, b"{}"

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        self.requests.append((method, parts.path))
        if parts.path == "/api/token/":
            return 200, {}, json.dumps({"access_token": self.token}).encode()
        if self.fail_on == (method, parts.path):
            if self.fail_skip == 0:
                return 500, {}, b"{}"
            self.fail_skip -= 1
        if method == "GET":
            return self._get(parts.path, parse_qs(parts.query))
        if method == "POST":
            return self._post(parts.path, body, headers)
        return self._put(parts.path, body)


def path_of(number: int) -> str:
    return f"{HISTORY}{number}/undo"


def writer_for(server: FakeServer) -> GrampsWriter:
    return GrampsWriter(BASE_URL, "editor", "secret", transport=server)


def references(*, source: bool = False) -> References:
    places = {
        name_key(BRINDLE): "pl-brindle",
        name_key("Marrow County, Eastmere"): "pl-marrow",
        name_key("Eastmere"): "pl-eastmere",
    }
    return References(places, {SOURCE: "src-existing"} if source else {})


def ardeth_born(year: int):
    return gramps_event("Birth", year, BRINDLE)


def world(tmp_path, *, source=False, unions=True):
    """Two linked people, the first with a wrong birth year, and a new daughter."""
    mine = tree(
        [
            gramps(
                "h1",
                "Ardeth",
                "Pellow",
                events=(ardeth_born(1901),),
                citations=(crosswalk("Ardeth Pellow"),),
            ),
            gramps(
                "h2",
                "Maren",
                "Voss",
                gender="female",
                citations=(crosswalk("Maren Voss"),),
            ),
        ]
    )
    people = his_people(
        his(
            1,
            "Ardeth",
            "Pellow",
            events=(his_event("born", 1902, BRINDLE),),
            unions=(his_union(10, 2, children=[3]),) if unions else (),
        ),
        his(
            2,
            "Maren",
            "Voss",
            sex="female",
            unions=(his_union(10, 1, children=[3]),) if unions else (),
        ),
        his(
            3,
            "Wren",
            "Pellow",
            sex="female",
            events=(his_event("born", 1960, "Oxbow Reach, Eastmere"),),
            parents=(ref(1), ref(2)),
        ),
    )
    matches = match_people(people, mine, SOURCE, set())
    inputs = Inputs(
        people,
        matches,
        mine,
        references(source=source),
        "example",
        TODAY,
        tmp_path / "media",
    )
    return people, mine, inputs, diff_people(people, mine, matches)


def accept_all(changes) -> dict[str, Decision]:
    return {c.id: Decision(ACCEPTED) for c in changes}


def build(tmp_path, **kwargs):
    _, _, inputs, changes = world(tmp_path, **kwargs)
    return build_plan(changes, accept_all(changes), inputs), inputs, changes


def objects_of(step):
    return {o["handle"]: o for o in step.body}


def test_the_writer_refuses_a_token_without_edit_rights():
    server = FakeServer(permissions=("ViewPrivate",))
    with pytest.raises(GrampsError, match="lacks AddObject, DeleteObject, EditObject"):
        writer_for(server).get("/api/metadata/")


@pytest.mark.parametrize("extra", ["ImportFile", "RepairTree", "BatchDeleteObjects"])
def test_the_writer_refuses_a_role_broader_than_editor(extra):
    server = FakeServer(permissions=(*EDITOR, extra))
    with pytest.raises(GrampsError, match=f"holds {extra}"):
        writer_for(server).get("/api/metadata/")


def test_the_read_client_still_only_warns_about_a_write_token():
    server = FakeServer()
    client = GrampsClient(BASE_URL, "reader", "secret", transport=server)
    client.get("/api/metadata/")
    assert "AddObject" in client.role_warnings()[0]


def test_the_plan_is_ordered_source_places_people_edits(tmp_path):
    plan, _, _ = build(tmp_path)
    kinds = [s.summary.split()[0] for s in plan.steps]
    assert kinds[0] == "create"
    assert "source" in plan.steps[0].summary
    assert "places" in plan.steps[1].summary
    assert plan.steps[2].summary == "create Wren Pellow"
    assert all(s.requires for s in plan.steps)


def test_the_source_and_repository_are_skipped_when_the_source_exists(tmp_path):
    plan, _, _ = build(tmp_path, source=True)
    assert not any("repository" in s.summary for s in plan.steps)
    created = objects_of(plan.steps[1])
    cites = [
        o
        for s in plan.steps
        if isinstance(s.body, list)
        for o in s.body
        if o["_class"] == "Citation"
    ]
    assert {c["source_handle"] for c in cites} == {"src-existing"}
    assert created


def test_places_are_reused_by_hierarchy_title_and_only_missing_ones_created(tmp_path):
    plan, _, _ = build(tmp_path)
    (places,) = [s for s in plan.steps if s.summary.startswith("create places")]
    (oxbow,) = places.body
    assert oxbow["name"]["value"] == "Oxbow Reach"
    assert oxbow["placeref_list"][0]["ref"] == "pl-eastmere"
    person = next(s for s in plan.steps if s.summary == "create Wren Pellow")
    birth = next(o for o in person.body if o["_class"] == "Event")
    assert birth["place"] == oxbow["handle"]


def test_a_new_person_is_one_transaction_with_citation_note_and_parent_family(tmp_path):
    plan, _, _ = build(tmp_path, unions=False)
    step = next(s for s in plan.steps if s.summary == "create Wren Pellow")
    classes = [o["_class"] for o in step.body]
    assert classes.count("Person") == classes.count("Family") == 1
    assert {"Citation", "Note", "Event"} <= set(classes)
    family = next(o for o in step.body if o["_class"] == "Family")
    wren = next(o for o in step.body if o["_class"] == "Person")
    assert {family["father_handle"], family["mother_handle"]} == {"h1", "h2"}
    assert [c["ref"] for c in family["child_ref_list"]] == [wren["handle"]]
    citation = next(o for o in step.body if o["_class"] == "Citation")
    assert citation["page"] == "Wren Pellow"
    assert citation["confidence"] == records.CONFIDENCE_LOW
    assert citation["attribute_list"][0]["value"].startswith("https://gw.geneanet.org/")
    assert citation["date"]["dateval"] == [8, 11, 2026, False]
    assert wren["citation_list"] == [citation["handle"]]
    assert step.requires == ("AddObject", "EditObject")


def test_the_parents_unions_do_not_create_the_family_twice(tmp_path):
    plan, _, _ = build(tmp_path)
    families = [
        o
        for s in plan.steps
        if isinstance(s.body, list)
        for o in s.body
        if o["_class"] == "Family"
    ]
    assert len(families) == 1


def test_a_changed_event_date_patches_the_event_and_attaches_a_citation(tmp_path):
    plan, _, _ = build(tmp_path)
    put = next(s for s in plan.steps if s.path == "/api/events/e-Birth-1901")
    assert isinstance(put.body, Patch)
    assert put.body.sets == (("date", records.date_json({"year": 1902})),)
    handles = [v for p, v in put.body.appends]
    assert len(handles) == 2
    before = plan.steps[plan.steps.index(put) - 1]
    assert {o["handle"] for o in before.body} == set(handles)


def test_an_edited_decision_replaces_the_proposed_value(tmp_path):
    _, _, inputs, changes = world(tmp_path)
    born = next(c for c in changes if c.field == "event:born")
    value = {"date": {"year": 1903}, "place": BRINDLE}
    decisions = {born.id: Decision(EDITED, value)}
    plan = build_plan(changes, decisions, inputs)
    put = next(s for s in plan.steps if s.path == "/api/events/e-Birth-1901")
    assert put.body.sets[0][1]["dateval"][2] == 1903


def test_rejected_and_undecided_changes_write_nothing(tmp_path):
    _, _, inputs, changes = world(tmp_path)
    decisions = {c.id: Decision(REJECTED) for c in changes[:1]}
    assert build_plan(changes, decisions, inputs).steps == ()


def test_only_accepted_media_is_uploaded_from_the_media_folder(tmp_path):
    picture = Media(7, "Mill", None, "https://img.test/x.jpg", "t", False)
    people, mine, inputs, _ = world(tmp_path)
    people[1] = his(
        1,
        "Ardeth",
        "Pellow",
        events=(his_event("born", 1901, BRINDLE),),
        media=(picture,),
    )
    changes = diff_people(people, mine, inputs.matches)
    media = next(c for c in changes if c.field == "media")
    plan = build_plan(changes, accept_all([media]), inputs)
    assert [
        s.summary for s in plan.steps if s.method == "POST" and "media" in s.path
    ] == []
    assert any("was not saved" in r for r in plan.skipped)
    file = tmp_path / "media" / media_path(1, picture)
    file.parent.mkdir(parents=True)
    file.write_bytes(b"jpegdata")
    plan = build_plan(changes, accept_all([media]), inputs)
    upload = next(s for s in plan.steps if isinstance(s.body, Upload))
    assert (upload.body.mime, upload.body.person) == ("image/jpeg", "h1")
    assert upload.requires == ("AddObject", "EditObject")
    phases = [isinstance(s.body, Upload) for s in plan.steps]
    assert phases.index(True) < len(phases) - 1


def test_the_preview_renders_the_same_steps_the_plan_holds(tmp_path):
    plan, _, _ = build(tmp_path)
    text = preview(plan)
    assert (
        text.splitlines()[0] == f"{len(plan.steps)} steps, {len(plan.steps)} to write"
    )
    for step in plan.steps:
        assert step.summary in text
        assert f"{step.method} {step.path}" in text
    first = (plan.steps[0].change_id, 0)
    assert "done" in preview(plan, {first}).splitlines()[1]


def test_apply_writes_in_plan_order_and_logs_each_step(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    written = apply(plan, writer_for(server), log)
    assert written == len(plan.steps)
    entries = read_log(log)
    assert [e["change"] for e in entries] == [s.change_id for s in plan.steps]
    assert all(e["transactions"] for e in entries)
    assert [t for e in entries for t in e["transactions"]] == server.transactions
    person_entry = next(e for e in entries if e["summary"] == "create Wren Pellow")
    assert any(p.startswith("/api/people/") for p in person_entry["created"])
    assert (
        "/api/events/e-Birth-1901"
        in next(e for e in entries if e["summary"].startswith("update the birth"))[
            "updated"
        ]
    )
    stored = server.store["/api/events/e-Birth-1901"]
    assert stored["date"]["dateval"][2] == 1902


def test_a_rerun_skips_logged_steps_and_writes_nothing(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    server.requests.clear()
    assert apply(plan, writer_for(server), log) == 0
    assert server.writes() == []
    assert len(read_log(log)) == len(plan.steps)


def test_a_rerun_stops_without_writing_when_a_written_value_drifted(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    server.store["/api/events/e-Birth-1901"]["date"]["dateval"] = [0, 0, 1999, False]
    server.requests.clear()
    with pytest.raises(DriftError, match="no longer holds"):
        apply(plan, writer_for(server), log)
    assert server.writes() == []


def test_a_rerun_stops_when_a_created_object_is_gone(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    gone = next(p for p in server.store if p.startswith("/api/people/gd"))
    del server.store[gone]
    with pytest.raises(DriftError, match="cannot be re-read"):
        apply(plan, writer_for(server), log)


def test_the_first_error_stops_the_run_and_a_rerun_resumes(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    server.fail_on = ("PUT", "/api/events/e-Birth-1901")
    with pytest.raises(GrampsError, match="HTTP 500"):
        apply(plan, writer_for(server), log)
    failed = next(i for i, s in enumerate(plan.steps) if s.path == server.fail_on[1])
    assert len(read_log(log)) == failed
    server.fail_on = None
    assert apply(plan, writer_for(server), log) == len(plan.steps) - failed
    assert len(read_log(log)) == len(plan.steps)


def test_an_upload_writes_the_file_names_it_and_attaches_it(tmp_path):
    picture = Media(7, "Mill", None, "https://img.test/x.jpg", "t", False)
    people, mine, inputs, _ = world(tmp_path)
    people[1] = his(
        1,
        "Ardeth",
        "Pellow",
        events=(his_event("born", 1901, BRINDLE),),
        media=(picture,),
    )
    file = tmp_path / "media" / media_path(1, picture)
    file.parent.mkdir(parents=True)
    file.write_bytes(b"jpegdata")
    changes = diff_people(people, mine, inputs.matches)
    media = next(c for c in changes if c.field == "media")
    plan = build_plan(changes, accept_all([media]), inputs)
    server = FakeServer(inputs.tree)
    apply(plan, writer_for(server), tmp_path / "applied.json")
    assert server.store["/api/media/media1"]["desc"] == "Mill"
    assert server.store["/api/media/media1"]["mime"] == "image/jpeg"
    assert server.store["/api/people/h1"]["media_list"][0]["ref"] == "media1"


def test_undo_reverts_the_newest_steps_newest_transaction_first(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    entries = read_log(log)
    server.requests.clear()
    assert undo(writer_for(server), log, 2) == 2
    undone = [p for m, p in server.requests if m == "UNDO"]
    expected = [t for e in reversed(entries[-2:]) for t in reversed(e["transactions"])]
    assert undone == [f"{HISTORY}{t}/undo" for t in expected]
    assert read_log(log) == entries[:-2]


GZIP = b"\x1f\x8b\x08"


def backup_client(payload: bytes, status=200):
    def transport(method, url, headers, body):
        if "token" in url:
            return (
                200,
                {},
                json.dumps({"access_token": make_token("ViewPrivate")}).encode(),
            )
        return status, {}, payload

    return GrampsClient(BASE_URL, "reader", "secret", transport=transport)


def test_backup_saves_the_export_privately(tmp_path):
    path = backup(
        backup_client(GZIP + b"xml"), tmp_path / "backups", datetime(2026, 11, 8, 9, 30)
    )
    assert path.name == "gramps-20261108T093000.gramps"
    assert path.read_bytes() == GZIP + b"xml"
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "client", [backup_client(b"{}", 500), backup_client(b"not gzip")]
)
def test_a_failed_backup_refuses_to_continue(tmp_path, client):
    with pytest.raises(GrampsError, match="nothing was written"):
        backup(client, tmp_path / "backups", datetime(2026, 11, 8))
    assert not list((tmp_path / "backups").glob("*"))


def test_write_credentials_follow_the_read_precedence(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text(
        'tree = "t"\ngramps_url = "http://g.test"\ngramps_write_user = "from-toml"\n'
    )
    cfg = load_config(str(data), {}, repo_root=tmp_path / "none")
    assert (cfg.gramps_write_user, cfg.gramps_write_password) == ("from-toml", None)
    (data / ".env").write_text(
        "GRAMPS_WRITE_USER=from-file\nGRAMPS_WRITE_PASSWORD=pw\n"
    )
    cfg = load_config(str(data), {}, repo_root=tmp_path / "none")
    assert (cfg.gramps_write_user, cfg.gramps_write_password) == ("from-file", "pw")
    cfg = load_config(
        str(data), {"GRAMPS_WRITE_USER": "from-env"}, repo_root=tmp_path / "none"
    )
    assert cfg.gramps_write_user == "from-env"
    assert "pw" not in repr(cfg)


def small_world(tmp_path, mine_people, his_list, families=()):
    mine = tree(mine_people, families)
    people = his_people(*his_list)
    matches = match_people(people, mine, SOURCE, set())
    inputs = Inputs(
        people, matches, mine, references(), "example", TODAY, tmp_path / "media"
    )
    changes = diff_people(people, mine, matches)
    return build_plan(changes, accept_all(changes), inputs), changes


def test_an_accepted_link_lets_a_new_person_name_that_parent(tmp_path):
    plan, changes = small_world(
        tmp_path,
        [
            gramps("h1", "Ardeth", "Pellow", citations=(crosswalk("Ardeth Pellow"),)),
            gramps("h2", "Maren", "Voss", gender="female"),
        ],
        [
            his(1, "Ardeth", "Pellow"),
            his(2, "Maren", "Voss", sex="female"),
            his(3, "Wren", "Pellow", parents=(ref(1), ref(2))),
        ],
    )
    assert any(c.field.startswith("link:") for c in changes)
    step = next(s for s in plan.steps if s.summary == "create Wren Pellow")
    family = next(o for o in step.body if o["_class"] == "Family")
    assert {family["father_handle"], family["mother_handle"]} == {"h1", "h2"}


def test_a_new_person_joins_the_parents_existing_family(tmp_path):
    plan, _ = small_world(
        tmp_path,
        [
            gramps(
                "h1",
                "Ardeth",
                "Pellow",
                citations=(crosswalk("Ardeth Pellow"),),
                families=("f1",),
            ),
            gramps(
                "h2",
                "Maren",
                "Voss",
                gender="female",
                citations=(crosswalk("Maren Voss"),),
                families=("f1",),
            ),
        ],
        [
            his(1, "Ardeth", "Pellow"),
            his(2, "Maren", "Voss", sex="female"),
            his(3, "Wren", "Pellow", parents=(ref(1), ref(2))),
        ],
        [family("f1", "h1", "h2")],
    )
    create = next(s for s in plan.steps if s.summary == "create Wren Pellow")
    assert not [o for o in create.body if o["_class"] == "Family"]
    wren = next(o["handle"] for o in create.body if o["_class"] == "Person")
    paths = {s.path: s.body for s in plan.steps if isinstance(s.body, Patch)}
    assert paths["/api/families/f1"].appends[0][1]["ref"] == wren
    assert paths[f"/api/people/{wren}"].appends == (("parent_family_list", "f1"),)


def test_scalar_differences_patch_the_person_with_evidence(tmp_path):
    plan, _ = small_world(
        tmp_path,
        [gramps("h1", "Ardeth", "Pellow", citations=(crosswalk("Ardeth Pellow"),))],
        [his(1, "Ardeth", "Pellow", sex="female", other_names=("Ard Pellow",))],
    )
    patches = {s.summary: s.body for s in plan.steps if isinstance(s.body, Patch)}
    gender = patches["set gender female for Ardeth Pellow"]
    assert gender.sets == (("gender", records.GENDER_VALUES["female"]),)
    assert [p for p, _ in gender.appends] == ["citation_list", "note_list"]
    other = patches["add the other name Ard Pellow for Ardeth Pellow"]
    assert other.appends[0][1]["first_name"] == "Ard"


def test_patches_are_idempotent_and_set_the_birth_index():
    obj = {"event_ref_list": [], "birth_ref_index": -1, "gender": 2}
    patch = Patch(
        sets=(("gender", 0),),
        appends=(("event_ref_list", records.event_ref("e1")),),
        indexes=(("birth_ref_index", "event_ref_list"),),
    )
    once = patched(obj, patch)
    again = patched(copy.deepcopy(once), patch)
    assert once == again
    assert once["birth_ref_index"] == 0
    stored = {**once, "event_ref_list": [{"ref": "e1", "role": "Primary"}]}
    assert holds(stored, patch)


def seed_log(path, transactions):
    entry = {
        "change": "c1",
        "index": 0,
        "summary": "s",
        "created": [],
        "updated": [],
        "transactions": list(transactions),
    }
    write_json(path, {"steps": [entry]})


def test_a_partial_undo_is_saved_per_transaction_and_resumes(tmp_path):
    server = FakeServer()
    for _ in range(3):
        server.commit()
    log = tmp_path / "applied.json"
    seed_log(log, [1, 2, 3])
    server.fail_on = ("POST", path_of(1))
    with pytest.raises(GrampsError, match="HTTP 500"):
        undo(writer_for(server), log, 1)
    assert [p for m, p in server.requests if m == "UNDO"] == [path_of(3), path_of(2)]
    assert read_log(log)[0]["transactions"] == [1]
    server.fail_on = None
    server.requests.clear()
    assert undo(writer_for(server), log, 1) == 1
    assert [p for m, p in server.requests if m == "UNDO"] == [path_of(1)]
    assert read_log(log) == []


def test_a_transaction_that_was_already_undone_does_not_stop_a_resume(tmp_path):
    server = FakeServer()
    server.commit(added=[("Note", "n1")])
    server.undone.add(1)
    log = tmp_path / "applied.json"
    seed_log(log, [1])
    assert undo(writer_for(server), log, 1) == 1
    assert read_log(log) == []


def test_a_failed_undo_of_an_object_that_still_exists_is_an_error(tmp_path):
    server = FakeServer()
    server.commit(added=[("Note", "n1")])
    server.undone.add(1)
    server.store["/api/notes/n1"] = {"handle": "n1"}
    log = tmp_path / "applied.json"
    seed_log(log, [1])
    with pytest.raises(GrampsError, match="failed"):
        undo(writer_for(server), log, 1)
    assert read_log(log)[0]["transactions"] == [1]


@pytest.mark.parametrize("last", [1, 2, 5])
def test_the_undo_preview_and_the_undo_select_the_same_steps(last):
    log = [{"change": str(i)} for i in range(3)]
    assert newest_steps(log, last) == log[-last:]


def parent_people(families=(), parent_families=()):
    return [
        gramps(
            "h1",
            "Ardeth",
            "Pellow",
            citations=(crosswalk("Ardeth Pellow"),),
            families=families,
        ),
        gramps(
            "h3",
            "Wren",
            "Pellow",
            gender="female",
            citations=(crosswalk("Wren Pellow"),),
            parent_families=parent_families,
        ),
    ]


PARENT_HIS = [
    his(1, "Ardeth", "Pellow"),
    his(3, "Wren", "Pellow", sex="female", parents=(ref(1),)),
]


def test_a_rerun_after_a_partial_parent_change_makes_no_second_family(tmp_path):
    first, _ = small_world(tmp_path, parent_people(), PARENT_HIS)
    parent_steps = [s for s in first.steps if not s.change_id.startswith("source")]
    assert len(parent_steps) == 3
    server = FakeServer(tree(parent_people()))
    log = tmp_path / "applied.json"
    server.fail_on = ("PUT", "/api/people/h3")
    with pytest.raises(GrampsError, match="HTTP 500"):
        apply(first, writer_for(server), log)
    assert len(read_log(log)) == len(first.steps) - 1
    made = records.handle_for("family", "h1")
    assert f"/api/families/{made}" in server.store
    second, _ = small_world(
        tmp_path,
        parent_people(families=(made,)),
        PARENT_HIS,
        [family(made, "h1", None, ["h3"])],
    )
    assert [(s.change_id, s.index) for s in second.steps] == [
        (s.change_id, s.index) for s in first.steps
    ]
    server.fail_on = None
    server.requests.clear()
    assert apply(second, writer_for(server), log) == 1
    assert server.writes() == [("PUT", "/api/people/h3")]
    assert [p for p in server.store if p.startswith("/api/families/")] == [
        f"/api/families/{made}"
    ]


def test_a_parent_joins_the_family_with_the_matching_spouse(tmp_path):
    mine = [
        gramps(
            "h1",
            "Ardeth",
            "Pellow",
            citations=(crosswalk("Ardeth Pellow"),),
            families=("f1", "f2"),
        ),
        gramps("h2", "Maren", "Voss", gender="female", families=("f1",)),
        gramps(
            "h5",
            "Quill",
            "Hartnoll",
            gender="female",
            citations=(crosswalk("Quill Hartnoll"),),
            families=("f2",),
        ),
        gramps(
            "h3",
            "Wren",
            "Pellow",
            citations=(crosswalk("Wren Pellow"),),
            gender="female",
        ),
    ]
    his_list = [
        his(1, "Ardeth", "Pellow"),
        his(5, "Quill", "Hartnoll", sex="female"),
        his(3, "Wren", "Pellow", sex="female", parents=(ref(1), ref(5))),
    ]
    plan, _ = small_world(
        tmp_path,
        mine,
        his_list,
        [family("f1", "h1", "h2"), family("f2", "h1", "h5")],
    )
    families = {s.path for s in plan.steps if s.path.startswith("/api/families/")}
    assert families == {"/api/families/f2"}


def test_an_event_with_nothing_to_set_writes_nothing(tmp_path):
    mine = [
        gramps(
            "h1",
            "Ardeth",
            "Pellow",
            events=(ardeth_born(1901),),
            citations=(crosswalk("Ardeth Pellow"),),
        )
    ]
    people = his_people(
        his(1, "Ardeth", "Pellow", events=(his_event("born", 1902, BRINDLE),))
    )
    snapshot = tree(mine)
    matches = match_people(people, snapshot, SOURCE, set())
    changes = diff_people(people, snapshot, matches)
    born = next(c for c in changes if c.field == "event:born")
    decisions = {born.id: Decision(EDITED, born.mine)}
    inputs = Inputs(people, matches, snapshot, references(), "example", TODAY, tmp_path)
    plan = build_plan(changes, decisions, inputs)
    assert plan.steps == ()
    assert any("nothing to set" in reason for reason in plan.skipped)


def test_alternate_names_never_raise():
    assert records.alternate_name("   ") is None
    single = records.alternate_name("Toby")
    assert single["first_name"] == "Toby"
    assert single["surname_list"][0]["surname"] == ""
    full = records.alternate_name("Tobias Pellowe")
    assert full["first_name"] == "Tobias"
    assert full["surname_list"][0]["surname"] == "Pellowe"


def test_a_blank_other_name_is_skipped_with_a_message(tmp_path):
    mine = [gramps("h1", "Ardeth", "Pellow", citations=(crosswalk("Ardeth Pellow"),))]
    people = his_people(his(1, "Ardeth", "Pellow", other_names=("Ard",)))
    snapshot = tree(mine)
    matches = match_people(people, snapshot, SOURCE, set())
    changes = diff_people(people, snapshot, matches)
    change = next(c for c in changes if c.field == "other_name")
    inputs = Inputs(people, matches, snapshot, references(), "example", TODAY, tmp_path)
    plan = build_plan(changes, {change.id: Decision(EDITED, "  ")}, inputs)
    assert plan.steps == ()
    assert any("blank" in reason for reason in plan.skipped)


def test_the_journal_records_only_our_own_transactions(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    original = server.commit

    def with_a_stranger(user="editor", added=()):
        original(user="someone-else")
        return original(user, added)

    server.commit = with_a_stranger
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    ours = {
        t["id"] for t in server.history if t["connection"]["user"]["name"] == "editor"
    }
    recorded = {t for e in read_log(log) for t in e["transactions"]}
    assert recorded == ours


def test_a_failed_journal_read_after_a_write_leaves_the_step_partial(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    server.fail_on = ("GET", HISTORY)
    server.fail_skip = 1
    log = tmp_path / "applied.json"
    with pytest.raises(GrampsError, match="HTTP 500"):
        apply(plan, writer_for(server), log)
    (entry,) = read_log(log)
    assert entry["partial"] is True
    assert entry["created"]
    server.fail_on = None
    with pytest.raises(DriftError, match="interrupted"):
        apply(plan, writer_for(server), log)


def test_a_drift_message_names_the_step_the_object_and_the_way_forward(tmp_path):
    plan, inputs, _ = build(tmp_path)
    server = FakeServer(inputs.tree)
    log = tmp_path / "applied.json"
    apply(plan, writer_for(server), log)
    server.store["/api/events/e-Birth-1901"]["date"]["dateval"] = [0, 0, 1999, False]
    with pytest.raises(DriftError) as raised:
        apply(plan, writer_for(server), log)
    text = str(raised.value)
    assert "/api/events/e-Birth-1901" in text
    assert "update the birth of Ardeth Pellow" in text
    assert "applied.json" in text
    assert "undo" in text
