from dataclasses import asdict
from pathlib import Path

from geneanet_drift.config import read_json, write_json_atomic
from geneanet_drift.gramps import (
    GrampsCitation,
    GrampsDate,
    GrampsError,
    GrampsEvent,
    GrampsFamily,
    GrampsFullPerson,
    GrampsMedia,
    GrampsNote,
    GrampsPlace,
    GrampsTree,
)

FORMAT = 3
THUMBNAILS_DIR = "thumbnails"


def _date(raw: dict | None) -> GrampsDate | None:
    return GrampsDate(**raw) if raw else None


def _citation(raw: dict) -> GrampsCitation:
    return GrampsCitation(**{**raw, "date": _date(raw["date"])})


def _event(raw: dict) -> GrampsEvent:
    return GrampsEvent(
        **{
            **raw,
            "date": _date(raw["date"]),
            "notes": tuple(GrampsNote(**n) for n in raw["notes"]),
            "citations": tuple(_citation(c) for c in raw["citations"]),
        }
    )


def _family(raw: dict) -> GrampsFamily:
    return GrampsFamily(
        **{
            **raw,
            "children": tuple(raw["children"]),
            "events": tuple(_event(e) for e in raw["events"]),
        }
    )


def _person(raw: dict) -> GrampsFullPerson:
    return GrampsFullPerson(
        **{
            **raw,
            "other_names": tuple(raw["other_names"]),
            "events": tuple(_event(e) for e in raw["events"]),
            "parent_families": tuple(raw["parent_families"]),
            "families": tuple(raw["families"]),
            "notes": tuple(GrampsNote(**n) for n in raw["notes"]),
            "citations": tuple(_citation(c) for c in raw["citations"]),
            "media": tuple(GrampsMedia(**m) for m in raw["media"]),
        }
    )


def tree_to_json(tree: GrampsTree) -> dict:
    return {"format": FORMAT, **asdict(tree)}


def tree_from_json(raw: dict) -> GrampsTree:
    try:
        if raw.get("format") != FORMAT:
            raise GrampsError(
                f"unsupported snapshot format {raw.get('format')!r}; "
                "run geneanet-drift snapshot again"
            )
        return GrampsTree(
            tuple(_person(p) for p in raw["people"]),
            tuple(_family(f) for f in raw["families"]),
            tuple(GrampsPlace(**p) for p in raw["places"]),
        )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise GrampsError(f"unreadable snapshot: {error!r}") from error


def write_tree(path: Path, tree: GrampsTree) -> None:
    write_json_atomic(path, tree_to_json(tree))


def read_tree(path: Path) -> GrampsTree:
    if not path.is_file():
        raise GrampsError(f"{path} does not exist")
    try:
        raw = read_json(path, {})
    except ValueError as error:
        raise GrampsError(f"unreadable snapshot {path}: {error}") from error
    return tree_from_json(raw)
