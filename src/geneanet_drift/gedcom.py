import argparse
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode, urlsplit

from geneanet_drift.capture import (
    Address,
    CaptureError,
    Date,
    Event,
    Media,
    Note,
    Person,
    Union,
    load_tree,
)
from geneanet_drift.config import ConfigError, load_config
from geneanet_drift.feed import tree_url

MAX_LINE = 255
GEDCOM_MONTHS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
DATE_PHRASES = {"about": "ABT", "before": "BEF"}
QUALITY_PHRASES = {"estimated": "EST"}
EVENT_TAGS = {"born": "BIRT", "deceased": "DEAT", "buried": "BURI", "killed": "DEAT"}
SEX_CODES = {"male": "M", "female": "F", "unknown": "U"}
SEX_ORDER = {"male": 0, "unknown": 1, "female": 2}
SOURCE_XREF = "@S1@"
SUBMITTER_XREF = "@U1@"
PROGRAM = "geneanet-drift"


class Export(NamedTuple):
    text: str
    people: int
    families: int
    skipped: int


class Family(NamedTuple):
    union: Union
    partners: tuple[int, ...]
    children: tuple[int, ...]
    skipped: int


def _person_xref(key: int) -> str:
    return f"@I{key}@"


def _family_xref(key: int) -> str:
    return f"@F{key}@"


def _chunk_end(text: str, start: int, width: int) -> int:
    end = min(start + width, len(text))
    while end < len(text) and end > start + 1 and text[end] in " @":
        end -= 1
    return end


def _chunks(text: str, first: int, rest: int) -> Iterator[str]:
    start, width = 0, first
    while True:
        end = _chunk_end(text, start, width)
        yield text[start:end]
        if end >= len(text):
            return
        start, width = end, rest


def _escape(text: str) -> str:
    return "@" + text if text.startswith("@") else text


def _line(level: int, tag: str, value: str = "", xref: str = "") -> str:
    head = f"{level} {xref + ' ' if xref else ''}{tag}"
    return f"{head} {value}" if value else head


def _lines(level: int, tag: str, text: str, xref: str = "") -> list[str]:
    paragraphs = text.replace("\r", "").split("\n")
    out: list[str] = []
    for number, paragraph in enumerate(paragraphs):
        head_tag = tag if number == 0 else "CONT"
        head_level = level if number == 0 else level + 1
        head_xref = xref if number == 0 else ""
        escaped = _escape(paragraph)
        room = MAX_LINE - len(_line(head_level, head_tag, "x", head_xref)) + 1
        pieces = list(_chunks(escaped, room, MAX_LINE - len(f"{level + 1} CONC ")))
        out.append(_line(head_level, head_tag, pieces[0], head_xref))
        out.extend(_line(level + 1, "CONC", piece) for piece in pieces[1:])
    return out


def format_date(date: Date) -> str:
    parts = [
        str(date.day) if date.day else "",
        GEDCOM_MONTHS[date.month - 1] if date.month else "",
        str(date.year),
    ]
    phrase = DATE_PHRASES.get(date.modifier or "") or QUALITY_PHRASES.get(
        date.quality or "", ""
    )
    return " ".join(p for p in [phrase, *parts] if p)


def _detail_lines(
    level: int, date: Date | None, place: str | None, extra: tuple[str, ...] = ()
) -> list[str]:
    lines = list(extra)
    if date:
        lines.extend(_lines(level, "DATE", format_date(date)))
    if place:
        lines.extend(_lines(level, "PLAC", place))
    return lines


def _event_lines(event: Event, deceased_emitted: bool) -> list[str]:
    detail = _detail_lines(2, event.date, event.place)
    if event.kind == "killed" and deceased_emitted:
        return [_line(1, "EVEN"), _line(2, "TYPE", "Killed"), *detail]
    tag = EVENT_TAGS[event.kind]
    cause = [_line(2, "CAUS", "Killed")] if event.kind == "killed" else []
    value = "" if detail or cause else "Y"
    return [_line(1, tag, value), *cause, *detail]


def _events_lines(events: tuple[Event, ...]) -> list[str]:
    ordered = sorted(events, key=lambda e: e.kind == "killed")
    lines: list[str] = []
    deceased = False
    for event in ordered:
        lines.extend(_event_lines(event, deceased))
        deceased = deceased or EVENT_TAGS[event.kind] == "DEAT"
    return lines


def _name_lines(person: Person) -> list[str]:
    lines = [
        _line(1, "NAME", f"{person.given} /{person.surname}/"),
        _line(2, "GIVN", person.given),
        _line(2, "SURN", person.surname),
    ]
    if person.public_name:
        lines.append(_line(2, "NICK", person.public_name))
    for other in person.other_names:
        lines.extend(
            [_line(1, "NAME", f"{other} //"), _line(2, "TYPE", "aka")],
        )
    return lines


def _note_lines(level: int, note: Note) -> list[str]:
    text = f"{note.kind}\n{note.text}" if note.kind else note.text
    return _lines(level, "NOTE", text)


def _page_url(tree: str, address: Address) -> str:
    query = {"n": address.surname, "p": address.given, "oc": address.occurrence}
    return f"{tree_url(tree)}?{urlencode(query)}"


def _citation_lines(tree: str, person: Person) -> list[str]:
    lines: list[str] = []
    if person.address:
        url = _page_url(tree, person.address)
        lines.extend([_line(1, "SOUR", SOURCE_XREF), *_lines(2, "PAGE", url)])
    for source in person.sources:
        lines.extend(
            [
                _line(1, "SOUR", SOURCE_XREF),
                _line(2, "DATA"),
                *_lines(3, "TEXT", f"{source.fact}: {source.text}"),
            ]
        )
    return lines


def media_path(person_key: int, media: Media) -> str:
    title = re.sub(r"[\W_]+", " ", media.title).strip()[:60].strip().replace(" ", "-")
    ident = re.sub(r"[^A-Za-z0-9]+", "", str(media.id))
    stem = f"{ident}-{title}" if title else ident
    return f"media/person-{person_key}/{stem}.{_extension(media.original)}"


def _extension(url: str) -> str:
    match = re.search(r"\.([A-Za-z0-9]{2,5})$", urlsplit(url).path)
    return match[1].lower() if match else "jpg"


def _media_lines(person: Person) -> list[str]:
    lines: list[str] = []
    for media in person.media:
        path = media_path(person.key, media)
        lines.extend([_line(1, "OBJE"), *_lines(2, "FILE", path)])
        lines.append(_line(3, "FORM", path.rsplit(".", 1)[1]))
        if media.title:
            lines.extend(_lines(3, "TITL", media.title))
    return lines


def _person_lines(
    tree: str,
    person: Person,
    parent_of: dict[int, list[int]],
    partner_in: dict[int, list[int]],
) -> list[str]:
    lines = [_line(0, "INDI", xref=_person_xref(person.key)), *_name_lines(person)]
    lines.append(_line(1, "SEX", SEX_CODES[person.sex]))
    lines.extend(_events_lines(person.events))
    if person.occupation:
        lines.extend(_lines(1, "OCCU", person.occupation))
    for note in person.notes:
        lines.extend(_note_lines(1, note))
    lines.extend(_citation_lines(tree, person))
    lines.extend(_media_lines(person))
    for fam in parent_of.get(person.key, []):
        lines.append(_line(1, "FAMC", _family_xref(fam)))
    for fam in partner_in.get(person.key, []):
        lines.append(_line(1, "FAMS", _family_xref(fam)))
    return lines


def _union_lines(union: Union) -> list[str]:
    has_detail = union.date or union.place
    if union.kind == "married":
        detail = _detail_lines(2, union.date, union.place)
        lines = [_line(1, "MARR", "" if has_detail else "Y"), *detail]
    else:
        lines = [
            _line(1, "EVEN"),
            _line(2, "TYPE", "Relationship"),
            *_detail_lines(2, union.date, union.place),
            *_lines(1, "NOTE", "Relationship (not a marriage)"),
        ]
    for note in union.notes:
        lines.extend(_note_lines(1, note))
    return lines


def _partner_tags(ordered: list[int], people: dict[int, Person]) -> list[str]:
    if len(ordered) == 1:
        return ["WIFE" if people[ordered[0]].sex == "female" else "HUSB"]
    return ["HUSB", "WIFE"][: len(ordered)]


def _family_lines(key: int, family: Family, people: dict[int, Person]) -> list[str]:
    ordered = sorted(family.partners, key=lambda k: (SEX_ORDER[people[k].sex], k))
    lines = [_line(0, "FAM", xref=_family_xref(key))]
    for tag, partner in zip(_partner_tags(ordered, people), ordered, strict=True):
        lines.append(_line(1, tag, _person_xref(partner)))
    lines.extend(_line(1, "CHIL", _person_xref(child)) for child in family.children)
    lines.extend(_union_lines(family.union))
    return lines


def _resolved(key: int | None, placeholder: bool, people: dict[int, Person]):
    return key if key in people and not placeholder else None


def _union_members(
    owner: int, union: Union, people: dict[int, Person]
) -> tuple[set[int], set[int], int]:
    spouse = _resolved(union.spouse.key, union.spouse.placeholder, people)
    children = [_resolved(c.key, c.placeholder, people) for c in union.children]
    partners = {owner} | ({spouse} if spouse is not None else set())
    kept = {c for c in children if c is not None}
    skipped = (spouse is None) + children.count(None)
    return partners, kept, skipped


def _collect_families(people: dict[int, Person]) -> dict[int, Family]:
    unions: dict[int, Union] = {}
    partners: dict[int, set[int]] = {}
    children: dict[int, set[int]] = {}
    skipped: dict[int, int] = {}
    for key in sorted(people):
        for union in people[key].unions:
            p, c, count = _union_members(key, union, people)
            unions.setdefault(union.key, union)
            skipped.setdefault(union.key, count)
            partners.setdefault(union.key, set()).update(p)
            children.setdefault(union.key, set()).update(c)
    return {
        fam: Family(
            union,
            tuple(sorted(partners[fam])),
            tuple(sorted(children[fam])),
            skipped[fam],
        )
        for fam, union in unions.items()
    }


def _index_families(
    families: dict[int, Family],
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    parent_of: dict[int, list[int]] = {}
    partner_in: dict[int, list[int]] = {}
    for fam in sorted(families):
        for child in families[fam].children:
            parent_of.setdefault(child, []).append(fam)
        for partner in families[fam].partners:
            partner_in.setdefault(partner, []).append(fam)
    return parent_of, partner_in


def _header_lines() -> list[str]:
    return [
        _line(0, "HEAD"),
        _line(1, "SOUR", PROGRAM),
        _line(2, "NAME", PROGRAM),
        _line(1, "GEDC"),
        _line(2, "VERS", "5.5.1"),
        _line(2, "FORM", "LINEAGE-LINKED"),
        _line(1, "CHAR", "UTF-8"),
        _line(1, "SUBM", SUBMITTER_XREF),
        _line(0, "SUBM", xref=SUBMITTER_XREF),
        _line(1, "NAME", PROGRAM),
    ]


def _source_lines(tree: str) -> list[str]:
    return [
        _line(0, "SOUR", xref=SOURCE_XREF),
        *_lines(1, "TITL", f"Geneanet tree {tree}"),
    ]


def export_gedcom(people: dict[int, Person], tree: str) -> Export:
    families = _collect_families(people)
    parent_of, partner_in = _index_families(families)
    lines = _header_lines()
    for key in sorted(people):
        lines.extend(_person_lines(tree, people[key], parent_of, partner_in))
    for fam in sorted(families):
        lines.extend(_family_lines(fam, families[fam], people))
    lines.extend(_source_lines(tree))
    lines.append(_line(0, "TRLR"))
    return Export(
        "\n".join(lines) + "\n",
        len(people),
        len(families),
        sum(f.skipped for f in families.values()),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="geneanet-drift gedcom",
        description="Export the captured Geneanet tree as GEDCOM 5.5.1.",
    )
    parser.add_argument("--data-dir", help="private data directory")
    parser.add_argument("--pages", type=Path, required=True, help="captured pages")
    parser.add_argument("--out", type=Path, required=True, help="GEDCOM file to write")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.data_dir, os.environ)
        result = export_gedcom(load_tree(args.pages, cfg.tree), cfg.tree)
    except (ConfigError, CaptureError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        args.out.write_text(result.text, encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"error: cannot write {args.out}: {error}", file=sys.stderr)
        return 1
    print(
        f"Wrote {args.out}: {result.people} people, {result.families} families, "
        f"{result.skipped} placeholder links skipped."
    )
    return 0
