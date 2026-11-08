"""Reads the seeded Gramps Web from tests/integration/compose.yaml; see seed.py."""

import json
import os
import time

import pytest

from geneanet_drift.gramps import (
    GrampsClient,
    GrampsDate,
    take_snapshot,
    take_tree,
    urllib_transport,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("GENEANET_DRIFT_INTEGRATION") != "1",
    reason="set GENEANET_DRIFT_INTEGRATION=1 with the compose stack running",
)

URL = os.environ.get("GENEANET_DRIFT_IT_URL", "http://127.0.0.1:18080")
SOURCE_TITLE = "Geneanet - example family tree"
# The token endpoint allows one request per second.
TOKEN_INTERVAL = 1.2


def _client(role: str) -> GrampsClient:
    user = os.environ.get(f"GENEANET_DRIFT_IT_{role.upper()}_USER", f"it-{role}")
    password = os.environ.get(
        f"GENEANET_DRIFT_IT_{role.upper()}_PASSWORD", f"it-{role}-pw"
    )
    time.sleep(TOKEN_INTERVAL)
    client = GrampsClient(URL, user, password)
    client.get("/api/metadata/")
    return client


@pytest.fixture(scope="module")
def reader() -> GrampsClient:
    return _client("reader")


@pytest.fixture(scope="module")
def editor() -> GrampsClient:
    return _client("editor")


@pytest.fixture(scope="module")
def tree(reader):
    return take_tree(reader)


def _person(tree, gramps_id):
    return next(p for p in tree.people if p.gramps_id == gramps_id)


def test_the_seeded_objects_are_all_read(tree, reader):
    counts, _ = reader.get("/api/metadata/")
    assert counts["object_counts"]["people"] == len(tree.people) == 5
    assert len(tree.families) == counts["object_counts"]["families"] == 1


def test_events_carry_dates_places_notes_and_citations(tree):
    ardeth = _person(tree, "I0001")
    birth, death, baptism = ardeth.events
    assert [e.kind for e in ardeth.events] == ["Birth", "Death", "Baptism"]
    assert birth.date == GrampsDate(1901, 3, 14, None, None, "")
    assert birth.description == "Born at the mill"
    assert birth.place == "Brindle Hollow, Marrow County, Eastmere"
    assert [n.text for n in birth.notes] == ["Birth date taken from the family bible."]
    assert [(c.source, c.page) for c in birth.citations] == [
        ("Parish register of Brindle Hollow", "folio 12")
    ]
    assert birth.citations[0].date == GrampsDate(1901, 4, 2, None, None, "")
    assert death.date == GrampsDate(1970, None, None, "about", None, "")
    assert (death.notes, death.citations) == ((), ())
    assert baptism.date.modifier == "span"
    assert (baptism.date.year, baptism.date.end_year) == (1901, 1901)
    assert baptism.place is None


def test_date_modifiers_qualities_and_ranges(tree):
    maren = _person(tree, "I0002")
    assert maren.events[0].date == GrampsDate(1905, None, None, "before", None, "")
    tobin = _person(tree, "I0003")
    assert tobin.events[0].date == GrampsDate(
        1930, None, None, "range", None, "", end_year=1931
    )
    marriage = tree.families[0].events[0]
    assert marriage.date == GrampsDate(1928, 6, None, None, "estimated", "")
    assert (
        marriage.place
        == "Saint Orsolya Chapel, Brindle Hollow, Marrow County, Eastmere"
    )


def test_a_witness_event_is_not_the_persons_own(tree):
    tobin = _person(tree, "I0003")
    assert [e.kind for e in tobin.events] == ["Birth"]


def test_person_fields(tree):
    tobin = _person(tree, "I0003")
    assert (tobin.given, tobin.surname, tobin.nickname) == ("Tobin", "Pellow", "Toby")
    assert tobin.other_names == ("Tobias Pellowe",)
    assert tobin.gender == "male"
    assert _person(tree, "I0002").gender == "female"
    assert _person(tree, "I0005").gender == "unknown"
    assert _person(tree, "I0004").parent_families == tobin.parent_families
    ardeth = _person(tree, "I0001")
    assert [n.text for n in ardeth.notes] == ["Ran the mill until the flood."]
    assert [(m.description, m.path, m.mime) for m in ardeth.media] == [
        ("Portrait of Ardeth", "portrait.jpg", "image/jpeg")
    ]
    assert [c.page for c in ardeth.citations] == ["Ardeth.0 Pellow"]


def test_family_links(tree):
    family = tree.families[0]
    by_handle = {p.handle: p.gramps_id for p in tree.people}
    assert by_handle[family.father] == "I0001"
    assert by_handle[family.mother] == "I0002"
    assert [by_handle[h] for h in family.children] == ["I0003", "I0004"]
    assert family.kind == "Married"
    assert _person(tree, "I0001").families == (family.handle,)


def test_snapshot_reads_years_and_links(reader):
    snapshot = take_snapshot(reader, SOURCE_TITLE)
    assert snapshot.families == 1
    years = {p.gramps_id: (p.birth_year, p.death_year) for p in snapshot.people}
    assert years["I0001"] == (1901, 1970)
    assert years["I0003"] == (1930, None)
    assert list(snapshot.links.values()) == ["I0001"]


def test_the_read_user_has_no_write_permissions(reader):
    assert reader.role_warnings() == []
    assert "ViewPrivate" in reader.permissions


def test_the_editor_is_warned_about_but_can_still_read(editor):
    assert editor.role_warnings()
    assert {"AddObject", "EditObject"} <= editor.permissions


def test_the_read_user_cannot_write(reader):
    token = reader._token
    body = json.dumps(
        [{"_class": "Note", "text": {"_class": "StyledText", "string": "x"}}]
    ).encode()
    status, _, _ = urllib_transport(
        "POST",
        f"{URL}/api/objects/",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        body,
    )
    assert status == 403
