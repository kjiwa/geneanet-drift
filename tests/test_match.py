import pytest

from geneanet_drift.gramps import GrampsPerson
from geneanet_drift.match import (
    WorkItem,
    load_negatives,
    name_candidates,
    narrow,
    normalise,
    year_verdict,
)

TARIK = GrampsPerson("h1", "I0001", "Tarik", "Zorvane", 1944, None)
BRANNOCH = GrampsPerson("h3", "I0003", "Brannoch", "Harrowmere", None, None)


def item(**years) -> WorkItem:
    return WorkItem(name="Tariq Zorvane", key="tariq zorvane", **years)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Tariq", "Tarik"),
        ("Zorvatch", "Zorvach"),
        ("Vantel-Quillon", "Vantel Quillon"),
        ("Hannah", "Hanah"),
    ],
)
def test_normalise_folds_spelling_variants(left, right):
    assert normalise(left) == normalise(right)


def test_fuzzy_match_catches_inserted_vowels():
    people = (TARIK, BRANNOCH)
    assert [p.gramps_id for p in name_candidates("Brannock Harrowmere", people)] == [
        "I0003"
    ]
    assert name_candidates("Completely Different", people) == []


def test_patronymic_chain_matches_the_shorter_name():
    people = (
        GrampsPerson("h1", "I1", "Brannock", "Harrowmere Odalys"),
        GrampsPerson("h2", "I2", "Brannock", "Zorvane"),
    )
    assert [p.gramps_id for p in name_candidates("Brannock Harrowmere", people)] == [
        "I1"
    ]
    # A single shared word is not enough to propose a match.
    assert name_candidates("Brannock", people) == []


def test_agreeing_birth_year_is_evidence():
    verdict = year_verdict(item(birth_year=1945), TARIK)
    assert (verdict.agree, verdict.conflict) == ("birth year agrees (1945)", "")


def test_a_year_two_apart_is_a_conflict():
    assert year_verdict(item(birth_year=1943), TARIK).conflict == ""
    assert year_verdict(item(birth_year=1946), TARIK).conflict == "birth 1946 vs 1944"


def test_a_death_year_the_tree_lacks_is_no_evidence():
    verdict = year_verdict(item(death_year=2011), TARIK)
    assert (verdict.agree, verdict.conflict) == ("", "")


def test_narrow_prefers_agreeing_then_non_conflicting_candidates():
    twin = GrampsPerson("h4", "I0004", "Tarek", "Zorvane", 1950, None)
    agree, conflict = (year_verdict(item(birth_year=1944), p) for p in (TARIK, twin))
    assert narrow([conflict, agree]) == [agree]
    neutral = year_verdict(item(), BRANNOCH)
    assert narrow([conflict, neutral]) == [neutral]
    assert narrow([conflict]) == [conflict]


def test_negatives_load_as_pairs_and_default_empty(tmp_path):
    path = tmp_path / "negatives.json"
    assert load_negatives(path) == set()
    path.write_text('[{"his": "ab cd", "gramps": "I0009"}]')
    assert load_negatives(path) == {("ab cd", "I0009")}
