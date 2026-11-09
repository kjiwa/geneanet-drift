from dataclasses import dataclass
from pathlib import Path

from geneanet_drift.config import read_json, write_json_atomic
from geneanet_drift.diff import Change
from geneanet_drift.feed import name_key

ACCEPTED = "accepted"
REJECTED = "rejected"
EDITED = "edited"
STATUSES = (ACCEPTED, REJECTED, EDITED)
LINK_PREFIX = "link:"
PLACE_PREFIX = "place:"


class DecisionError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    status: str
    value: object = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise DecisionError(f"unknown decision status {self.status!r}")
        if (self.status == EDITED) != (self.value is not None):
            raise DecisionError("an edited decision needs a value, others take none")


def load_decisions(path: Path) -> dict[str, Decision]:
    try:
        return {
            change_id: Decision(row["status"], row.get("value"))
            for change_id, row in read_json(path, {}).items()
        }
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise DecisionError(f"{path} is not a decisions file: {error!r}") from error


def save_decisions(path: Path, decisions: dict[str, Decision]) -> None:
    write_json_atomic(
        path,
        {
            change_id: {"status": d.status, "value": d.value}
            for change_id, d in sorted(decisions.items())
        },
    )


def decide(
    decisions: dict[str, Decision], change_id: str, decision: Decision
) -> dict[str, Decision]:
    return {**decisions, change_id: decision}


def link_negatives(
    changes: list[Change], decisions: dict[str, Decision]
) -> set[tuple[str, str]]:
    """Name key of his person paired with the Gramps ID whose link was rejected."""
    return {
        (name_key(c.his), c.field.removeprefix(LINK_PREFIX))
        for c in changes
        if c.field.startswith(LINK_PREFIX) and decisions.get(c.id) == Decision(REJECTED)
    }
