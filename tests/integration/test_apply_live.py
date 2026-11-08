"""Applies a plan to a throwaway Gramps Web, then re-reads and undoes it.

Starts its own compose project (geneanet-drift-it, port 18080), seeds it, and
removes it afterwards. A stack that was already running is recreated and
reseeded on exit so test_live.py still finds a pristine tree.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pytest

from geneanet_drift.apply import (
    Inputs,
    apply,
    backup,
    build_plan,
    read_log,
    undo,
)
from geneanet_drift.capture import Date, Event, Media
from geneanet_drift.config import write_json
from geneanet_drift.decisions import ACCEPTED, Decision
from geneanet_drift.diff import diff_people
from geneanet_drift.gedcom import media_path
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsWriter,
    take_references,
    take_tree,
    urllib_transport,
)
from geneanet_drift.match_tree import match_people
from tests.builders import his, his_people, his_union, ref

pytestmark = pytest.mark.skipif(
    os.environ.get("GENEANET_DRIFT_INTEGRATION") != "1",
    reason="set GENEANET_DRIFT_INTEGRATION=1 with docker available",
)

HERE = Path(__file__).parent
COMPOSE = ["docker", "compose", "-f", str(HERE / "compose.yaml")]
URL = "http://127.0.0.1:18080"
SOURCE_TITLE = "Geneanet - example family tree"
BRINDLE = "Brindle Hollow, Marrow County, Eastmere"
MARROW = "Marrow County, Eastmere"
CHAPEL = f"Saint Orsolya Chapel, {BRINDLE}"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6e4f40000000049454e44ae426082"
)
# The token endpoint allows one request per second.
TOKEN_INTERVAL = 1.2
READY_SECONDS = 180


def _stack_is_running() -> bool:
    result = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _wait_until_ready() -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{URL}/api/metadata/", timeout=5)
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    raise RuntimeError("the Gramps Web stack did not come up")


def _fresh_stack() -> None:
    subprocess.run([*COMPOSE, "down", "-v"], check=True)
    subprocess.run([*COMPOSE, "up", "-d"], check=True)
    _wait_until_ready()
    subprocess.run([sys.executable, str(HERE / "seed.py")], check=True)


@pytest.fixture(scope="module", autouse=True)
def stack():
    was_running = _stack_is_running()
    _fresh_stack()
    yield
    subprocess.run([*COMPOSE, "down", "-v"], check=True)
    if was_running:
        _fresh_stack()


class Recorder:
    def __init__(self):
        self.writes: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        if method in ("POST", "PUT") and "/api/token/" not in url:
            self.writes.append((method, url))
        return urllib_transport(method, url, headers, body)


def _login(cls, user, transport=urllib_transport):
    time.sleep(TOKEN_INTERVAL)
    client = cls(URL, user, f"{user}-pw", transport=transport)
    client.get("/api/metadata/")
    return client


@pytest.fixture(scope="module")
def reader():
    return _login(GrampsClient, "it-reader")


@pytest.fixture(scope="module")
def recorder():
    return Recorder()


@pytest.fixture(scope="module")
def writer(recorder):
    return _login(GrampsWriter, "it-editor", recorder)


def _event(kind, text, year, month=None, day=None, modifier=None, place=None):
    return Event(kind, Date(text, year, month, day, modifier, None), place)


def _captured(picture: Media):
    """His tree: the seeded people with one wrong death year, one new person."""
    marriage = Date("1928, June", 1928, 6, None, None, "estimated")
    return his_people(
        his(
            1,
            "Ardeth",
            "Pellow",
            events=(
                _event("born", "March 14, 1901", 1901, 3, 14, place=BRINDLE),
                _event("deceased", "about 1971", 1971, modifier="about", place=MARROW),
            ),
            unions=(his_union(10, 2, children=[3, 4], date=marriage, place=CHAPEL),),
            media=(
                Media(
                    1, "Portrait of Ardeth", None, "https://img.test/p.jpg", "", True
                ),
                picture,
            ),
        ),
        his(
            2,
            "Maren",
            "Voss",
            sex="female",
            events=(
                _event("born", "before 1905", 1905, modifier="before", place=MARROW),
            ),
            unions=(his_union(10, 1, children=[3, 4], date=marriage, place=CHAPEL),),
        ),
        his(
            3,
            "Tobin",
            "Pellow",
            other_names=("Tobias Pellowe",),
            events=(_event("born", "1930", 1930, modifier="range", place=BRINDLE),),
            parents=(ref(1), ref(2)),
            unions=(his_union(11, 5, children=[6]),),
        ),
        his(
            4,
            "Isolde",
            "Pellow",
            sex="female",
            events=(_event("born", "November 2, 1933", 1933, 11, 2),),
            parents=(ref(1), ref(2)),
        ),
        his(
            5,
            "Quill",
            "Hartnoll",
            sex="unknown",
            unions=(his_union(11, 3, children=[6]),),
        ),
        his(
            6,
            "Wren",
            "Pellow",
            sex="female",
            events=(_event("born", "1960", 1960, place="Oxbow Reach, Eastmere"),),
            parents=(ref(3), ref(5)),
        ),
    )


def _plan(reader, media_dir: Path, picture: Media):
    tree = take_tree(reader)
    people = _captured(picture)
    matches = match_people(people, tree, SOURCE_TITLE, set())
    changes = diff_people(people, tree, matches)
    inputs = Inputs(
        people,
        matches,
        tree,
        take_references(reader),
        "example",
        date.today(),
        media_dir,
    )
    decisions = {c.id: Decision(ACCEPTED) for c in changes}
    return build_plan(changes, decisions, inputs), changes, tree


@pytest.fixture(scope="module")
def world(reader, tmp_path_factory):
    media_dir = tmp_path_factory.mktemp("capture")
    picture = Media(2, "Mill photo", None, "https://img.test/mill.png", "", False)
    file = media_dir / media_path(1, picture)
    file.parent.mkdir(parents=True)
    file.write_bytes(PNG)
    plan, changes, before = _plan(reader, media_dir, picture)
    return {
        "plan": plan,
        "changes": changes,
        "before": before,
        "media_dir": media_dir,
        "picture": picture,
        "log": tmp_path_factory.mktemp("data") / "applied.json",
    }


def test_the_diff_finds_the_intended_changes(world):
    fields = {c.field for c in world["changes"]}
    assert {"person", "event:deceased", "media"} <= fields
    assert world["plan"].skipped == ()
    assert any(s.summary == "create Wren Pellow" for s in world["plan"].steps)


def test_backup_saves_the_whole_tree(reader, tmp_path):
    path = backup(reader, tmp_path / "backups", datetime.now())
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    assert path.stat().st_size > 100


def test_apply_writes_the_plan_and_the_reread_tree_has_no_mismatch(
    world, writer, reader
):
    written = apply(world["plan"], writer, world["log"])
    assert written == len(world["plan"].steps)
    entries = read_log(world["log"])
    assert all(e["transactions"] for e in entries)
    after = take_tree(reader)
    picture = world["picture"]
    people = _captured(picture)
    matches = match_people(people, after, SOURCE_TITLE, set())
    mismatches = diff_people(people, after, matches)
    shown = [f"{c.person} {c.field} {c.kind} {c.his} | {c.mine}" for c in mismatches]
    assert not mismatches, "\n".join(shown)


def test_the_written_facts_carry_citations_and_files(world, reader):
    after = take_tree(reader)
    by_name = {f"{p.given} {p.surname}": p for p in after.people}
    wren = by_name["Wren Pellow"]
    assert [(c.source, c.page) for c in wren.citations] == [
        (SOURCE_TITLE, "Wren Pellow")
    ]
    assert any("Applied from Geneanet" in n.text for n in wren.notes)
    ardeth = by_name["Ardeth Pellow"]
    death = next(e for e in ardeth.events if e.kind == "Death")
    assert (death.date.year, death.date.modifier) == (1971, "about")
    assert [c.source for c in death.citations] == [SOURCE_TITLE]
    uploaded = next(m for m in ardeth.media if m.description == "Mill photo")
    assert uploaded.mime == "image/png"
    families = {f.handle: f for f in after.families}
    parents = families[wren.parent_families[0]]
    assert {parents.father, parents.mother} == {
        by_name["Tobin Pellow"].handle,
        by_name["Quill Hartnoll"].handle,
    }


def test_a_rerun_is_a_no_op(world, writer, recorder, reader):
    recorder.writes.clear()
    assert apply(world["plan"], writer, world["log"]) == 0
    assert recorder.writes == []
    fresh, changes, _ = _plan(reader, world["media_dir"], world["picture"])
    assert (fresh.steps, changes) == ((), [])


def test_undo_restores_the_tree(world, writer, reader):
    entries = read_log(world["log"])
    assert undo(writer, world["log"], len(entries)) == len(entries)
    assert read_log(world["log"]) == []
    assert take_tree(reader) == world["before"]
    # An interrupted undo may be rerun over a transaction that is already undone.
    write_json(world["log"], {"steps": [entries[0]]})
    assert undo(writer, world["log"], 1) == 1
    assert take_tree(reader) == world["before"]
