from dataclasses import dataclass
from pathlib import Path

from geneanet_drift.config import read_json, write_json_atomic
from geneanet_drift.diff import LINK_PREFIX, Change
from geneanet_drift.feed import name_key

ACCEPTED = "accepted"
REJECTED = "rejected"
EDITED = "edited"
STATUSES = (ACCEPTED, REJECTED, EDITED)
PLACE_PREFIX = "place:"
NONE_PREFIX = "none:"


class DecisionError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    status: str
    value: object = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise DecisionError(f'unknown decision status "{self.status}"')
        if (self.status == EDITED) != (self.value is not None):
            raise DecisionError("an edited decision needs a value, others take none")


def load_decisions(path: Path) -> dict[str, Decision]:
    try:
        return {
            change_id: Decision(row["status"], row.get("value"))
            for change_id, row in read_json(path, {}).items()
        }
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise DecisionError(f"{path} is not a decisions file: {error}") from error


def save_decisions(path: Path, decisions: dict[str, Decision]) -> None:
    write_json_atomic(
        path,
        {
            change_id: {"status": d.status, "value": d.value}
            for change_id, d in sorted(decisions.items())
        },
    )


def link_negatives(
    changes: list[Change], decisions: dict[str, Decision]
) -> set[tuple[str, str]]:
    """Name key of his person paired with the Gramps ID whose link was rejected."""
    return {
        (name_key(c.his), c.field.removeprefix(LINK_PREFIX))
        for c in changes
        if c.field.startswith(LINK_PREFIX) and decisions.get(c.id) == Decision(REJECTED)
    }


def refused_people(decisions: dict[str, Decision]) -> frozenset[int]:
    """Keys of his people the operator said match nobody in Gramps."""
    return frozenset(
        int(key.removeprefix(NONE_PREFIX))
        for key, d in decisions.items()
        if key.startswith(NONE_PREFIX)
        and d == Decision(REJECTED)
        and key.removeprefix(NONE_PREFIX).isdigit()
    )
