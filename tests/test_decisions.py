import json
import os
import stat

import pytest

from geneanet_drift.decisions import (
    ACCEPTED,
    EDITED,
    REJECTED,
    Decision,
    DecisionError,
    link_negatives,
    load_decisions,
    save_decisions,
)
from geneanet_drift.diff import NEW_PERSON, NEW_VALUE, Change, change_id


def change(field: str, his="Rowan Ashby", kind=NEW_VALUE) -> Change:
    return Change(change_id(1, field, his), 1, field, kind, his, None, None)


def test_missing_file_means_no_decisions(tmp_path):
    assert load_decisions(tmp_path / "decisions.json") == {}


def test_decisions_round_trip(tmp_path):
    path = tmp_path / "decisions.json"
    decisions = {
        "a": Decision(ACCEPTED),
        "b": Decision(REJECTED),
        "c": Decision(EDITED, {"year": 1951}),
    }
    save_decisions(path, decisions)
    assert load_decisions(path) == decisions


@pytest.mark.parametrize(
    "build",
    [
        lambda: Decision("maybe"),
        lambda: Decision(EDITED),
        lambda: Decision(ACCEPTED, "value"),
    ],
)
def test_invalid_decisions_are_refused(build):
    with pytest.raises(DecisionError):
        build()


@pytest.mark.parametrize(
    "text", ["[]", "{not json", '{"a": {"value": 1}}', '{"a": {"status": "x"}}']
)
def test_malformed_file_raises(tmp_path, text):
    path = tmp_path / "decisions.json"
    path.write_text(text)
    with pytest.raises(DecisionError):
        load_decisions(path)


def test_write_is_private_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "decisions.json"
    save_decisions(path, {"a": Decision(ACCEPTED)})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["decisions.json"]


def test_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "decisions.json"
    save_decisions(path, {"a": Decision(ACCEPTED)})
    before = path.read_text()

    def fail(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        save_decisions(path, {"b": Decision(REJECTED)})
    assert path.read_text() == before
    assert json.loads(before) == {"a": {"status": ACCEPTED, "value": None}}
    assert [p.name for p in tmp_path.iterdir()] == ["decisions.json"]


def test_rejected_links_become_negative_pairs():
    link = change("link:I0001", his="Rowan  Ashby")
    kept = change("link:I0002", his="Rowan Ashby")
    other = change("given", his="Rowan")
    person = change("person", kind=NEW_PERSON)
    decisions = {
        link.id: Decision(REJECTED),
        kept.id: Decision(ACCEPTED),
        other.id: Decision(REJECTED),
        person.id: Decision(REJECTED),
    }
    assert link_negatives([link, kept, other, person], decisions) == {
        ("rowan ashby", "I0001")
    }


def test_write_syncs_the_file_before_replacing_it(tmp_path, monkeypatch):
    order = []
    real_replace = os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: order.append("fsync"))
    monkeypatch.setattr(
        os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
    )
    save_decisions(tmp_path / "decisions.json", {"a": Decision(ACCEPTED)})
    assert order == ["fsync", "replace"]


def test_a_refused_status_is_quoted_not_shown_as_a_python_repr():
    with pytest.raises(DecisionError) as raised:
        Decision("maybe")
    assert str(raised.value) == 'unknown decision status "maybe"'
