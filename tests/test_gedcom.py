import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from geneanet_drift.capture import (
    Address,
    Date,
    Event,
    Media,
    Note,
    Person,
    Source,
    load_tree,
)
from geneanet_drift.gedcom import MAX_LINE, export_gedcom, format_date, main

GENEANET = Path(__file__).parent / "fixtures" / "geneanet"
TREE = "exampletree"
LINE = re.compile(r"^(\d+) (?:(@\w+@) )?(\w+)(?: (.*))?$")


def person(**changes) -> Person:
    base = Person(
        key=1,
        given="Ada",
        surname="Quill",
        public_name=None,
        other_names=(),
        sex="female",
        occupation=None,
        events=(),
        parents=(),
        unions=(),
        notes=(),
        sources=(),
        media=(),
        address=None,
    )
    return replace(base, **changes)


def date(
    year=1900, month=None, day=None, modifier=None, quality=None, text="x"
) -> Date:
    return Date(text, year, month, day, modifier, quality)


def parse_lines(text: str) -> list[tuple[int, str | None, str, str]]:
    parsed = []
    for raw in text.split("\n")[:-1]:
        assert len(raw) <= MAX_LINE
        match = LINE.match(raw)
        assert match, raw
        parsed.append((int(match[1]), match[2], match[3], match[4] or ""))
    return parsed


def logical(parsed) -> list[tuple[int, str | None, str, str]]:
    merged: list[list] = []
    for level, xref, tag, value in parsed:
        if tag == "CONC":
            merged[-1][3] += value
        elif tag == "CONT":
            merged[-1][3] += "\n" + value
        else:
            merged.append([level, xref, tag, value])
    return [tuple(m) for m in merged]


def records(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for level, xref, tag, _ in parse_lines(text):
        if level == 0 and xref:
            found.setdefault(tag, []).append(xref)
    return found


def body(text: str, xref: str) -> list[str]:
    out, inside = [], False
    for raw in text.split("\n"):
        if raw.startswith("0 "):
            inside = raw.startswith(f"0 {xref} ")
        elif inside:
            out.append(raw)
    return out


@pytest.fixture
def tree(tmp_path):
    shutil.copytree(GENEANET / "tree", tmp_path / "pages")
    return load_tree(tmp_path / "pages", TREE)


def test_fixture_tree_structure_and_cross_references(tree):
    result = export_gedcom(tree, TREE)
    parsed = parse_lines(result.text)
    assert parsed[0][2] == "HEAD" and parsed[-1][2] == "TRLR"
    found = records(result.text)
    assert len(found["INDI"]) == result.people == 5
    assert len(found["FAM"]) == result.families == 2
    assert result.skipped == 1
    defined = {x for xs in found.values() for x in xs}
    pointers = [v for _, _, _, v in parsed if re.fullmatch(r"@\w+@", v)]
    assert pointers and set(pointers) <= defined
    assert body(result.text, "@F1@")[:3] == [
        "1 HUSB @I2@",
        "1 WIFE @I1@",
        "1 CHIL @I3@",
    ]
    assert "1 FAMC @F1@" in body(result.text, "@I3@")
    assert "1 FAMS @F1@" in body(result.text, "@I1@")
    assert "1 FAMS @F2@" in body(result.text, "@I5@")
    assert "2 PAGE https://gw.geneanet.org/exampletree_w?n=quill&p=ada&oc=0" in body(
        result.text, "@I1@"
    )


def test_unmarried_union_is_an_event_with_a_note(tree):
    family = body(export_gedcom(tree, TREE).text, "@F2@")
    assert family[0] == "1 HUSB @I5@"
    assert "1 EVEN" in family and "2 TYPE Relationship" in family
    assert "1 NOTE Relationship (not a marriage)" in family


def test_levels_never_jump(tree):
    previous = 0
    for level, _, _, _ in parse_lines(export_gedcom(tree, TREE).text):
        assert level <= previous + 1
        previous = level


@pytest.mark.parametrize(
    ("kwargs", "phrase"),
    [
        ({}, "1900"),
        ({"month": 3}, "MAR 1900"),
        ({"month": 3, "day": 7}, "7 MAR 1900"),
        ({"modifier": "about", "month": 3, "day": 7}, "ABT 7 MAR 1900"),
        ({"modifier": "before"}, "BEF 1900"),
        ({"quality": "estimated", "month": 12}, "EST DEC 1900"),
    ],
)
def test_date_phrases(kwargs, phrase):
    assert format_date(date(**kwargs)) == phrase


def test_events_map_to_tags_with_killed_as_death_cause():
    events = (
        Event("born", date(month=3), "Harbor"),
        Event("buried", None, None),
        Event("killed", date(1944), None),
    )
    text = export_gedcom({1: person(events=events)}, TREE).text
    lines = body(text, "@I1@")
    assert "1 BIRT" in lines and "2 DATE MAR 1900" in lines and "2 PLAC Harbor" in lines
    assert "1 BURI Y" in lines
    assert "1 DEAT" in lines and "2 CAUS Killed" in lines and "2 DATE 1944" in lines


def test_killed_after_deceased_is_a_typed_event():
    events = (Event("killed", date(1944), None), Event("deceased", date(1945), None))
    lines = body(export_gedcom({1: person(events=events)}, TREE).text, "@I1@")
    assert lines.count("1 DEAT") == 1
    assert "1 EVEN" in lines and "2 TYPE Killed" in lines


def test_names_notes_sources_media_and_url():
    p = person(
        public_name="Addie",
        other_names=("Ada Q",),
        occupation="Miller",
        notes=(Note("Bio", "line one\nline two"),),
        sources=(Source("Birth", "Parish book"),),
        media=(Media(1001, "Harbor, 1975", None, "//h/x/normal.JPG?t=1", "t", True),),
        address=Address("quill", "ada", 2),
    )
    lines = body(export_gedcom({1: p}, TREE).text, "@I1@")
    assert "1 NAME Ada /Quill/" in lines and "2 NICK Addie" in lines
    assert "1 NAME Ada Q //" in lines and "2 TYPE aka" in lines
    assert "1 OCCU Miller" in lines
    assert "1 NOTE Bio" in lines and "2 CONT line one" in lines
    assert "3 TEXT Birth: Parish book" in lines
    assert "2 PAGE https://gw.geneanet.org/exampletree_w?n=quill&p=ada&oc=2" in lines
    assert "2 FILE media/person-1/1001-Harbor-1975.jpg" in lines
    assert "3 FORM jpg" in lines and "2 TITL Harbor, 1975" in lines


def test_long_values_split_with_conc_and_cont_and_roundtrip():
    long_text = "word " * 150 + "tail"
    note = Note("", long_text + "\n@handle and more\n\nlast")
    parsed = parse_lines(export_gedcom({1: person(notes=(note,))}, TREE).text)
    assert any(tag == "CONC" for _, _, tag, _ in parsed)
    assert any(tag == "CONT" for _, _, tag, _ in parsed)
    (value,) = [v for _, _, tag, v in logical(parsed) if tag == "NOTE"]
    assert value == long_text + "\n@@handle and more\n\nlast"


def test_conc_never_starts_with_space_or_at():
    note = Note("", "a" * 240 + " @ " + "b" * 300)
    parsed = parse_lines(export_gedcom({1: person(notes=(note,))}, TREE).text)
    for _, _, tag, value in parsed:
        if tag == "CONC":
            assert not value.startswith((" ", "@"))


def test_cli_writes_file_and_reports_counts(tmp_path, capsys):
    shutil.copytree(GENEANET / "tree", tmp_path / "pages")
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text(
        f'tree = "{TREE}"\ngramps_url = "http://gramps.test"\n'
    )
    out = tmp_path / "tree.ged"
    args = ["--data-dir", str(data), "--pages", str(tmp_path / "pages")]
    assert main([*args, "--out", str(out)]) == 0
    assert "5 people, 2 families, 1 placeholder" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8").endswith("0 TRLR\n")
