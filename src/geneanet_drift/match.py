import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path

from geneanet_drift.config import read_json
from geneanet_drift.gramps import GrampsPerson

NAME_SIMILARITY = 0.9
MIN_CONTAINED_WORDS = 2
YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class WorkItem:
    name: str
    key: str
    birth_year: int | None = None
    death_year: int | None = None


@dataclass(frozen=True)
class Verdict:
    person: GrampsPerson
    agree: str
    conflict: str


@cache
def normalise(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    letters = re.sub(r"[^a-z ]", "", re.sub(r"[-.]", " ", ascii_name.casefold()))
    letters = re.sub(r"(?:tch|kch|ch)", "ch", letters.replace("q", "k"))
    return re.sub(r"(.)\1+", r"\1", " ".join(letters.split()))


def _same_name(wanted: str, other: str) -> bool:
    if SequenceMatcher(None, wanted, other).ratio() >= NAME_SIMILARITY:
        return True
    # Patronymic chains: one tree may record "Given Father" where the other
    # records "Given Father Grandfather".
    shorter, longer = sorted((set(wanted.split()), set(other.split())), key=len)
    return len(shorter) >= MIN_CONTAINED_WORDS and shorter <= longer


@cache
def _cached_candidates(
    name: str, people: tuple[GrampsPerson, ...]
) -> tuple[GrampsPerson, ...]:
    wanted = normalise(name)
    return tuple(p for p in people if _same_name(wanted, normalise(p.name)))


def name_candidates(name: str, people: tuple[GrampsPerson, ...]) -> list[GrampsPerson]:
    """Cached: review re-matches after every choice, and names never change."""
    return list(_cached_candidates(name, people))


def year_verdict(item: WorkItem, person: GrampsPerson) -> Verdict:
    pairs = (
        ("birth", item.birth_year, person.birth_year),
        ("death", item.death_year, person.death_year),
    )
    known = [(label, his, yours) for label, his, yours in pairs if his and yours]
    agree = [
        f"{label} year agrees ({his})"
        for label, his, yours in known
        if abs(his - yours) <= YEAR_TOLERANCE
    ]
    conflict = [
        f"{label} {his} vs {yours}"
        for label, his, yours in known
        if abs(his - yours) > YEAR_TOLERANCE
    ]
    return Verdict(person, "; ".join(agree), "; ".join(conflict))


def narrow(verdicts: list[Verdict]) -> list[Verdict]:
    agreeing = [v for v in verdicts if v.agree and not v.conflict]
    if agreeing:
        return agreeing
    return [v for v in verdicts if not v.conflict] or verdicts


def load_negatives(path: Path) -> set[tuple[str, str]]:
    return {(row["his"], row["gramps"]) for row in read_json(path, [])}
