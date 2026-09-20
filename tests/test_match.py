from dataclasses import replace
from datetime import datetime

import pytest

from geneanet_drift.config import end_of_day
from geneanet_drift.feed import Entry, parse_feed, since
from geneanet_drift.gramps import GrampsPerson, Snapshot, take_snapshot
from geneanet_drift.match import (
    AMBIGUOUS,
    CONFIRMED,
    LINKED,
    NONE,
    PROBABLE,
    build_clusters,
    build_notices,
    combine,
    is_placeholder,
    load_negatives,
    load_pending,
    name_candidates,
    needing_decision,
    normalise,
    save_negatives,
    save_pending,
    work_items,
)
from tests.conftest import make_client

TITLE = "Geneanet - example family tree"
CURSOR = end_of_day(datetime(2026, 4, 2).date())


@pytest.fixture
def snapshot(client):
    return take_snapshot(client, TITLE)


@pytest.fixture
def items(feed_text):
    return work_items(since(parse_feed(feed_text, "example"), CURSOR))


def tiers(clusters):
    return {r.item.name: r.tier for c in clusters for r in c.resolutions}


def with_twin(fake_gramps, birth=None):
    twin = {
        "handle": "h4",
        "gramps_id": "I0004",
        "primary_name": {
            "first_name": "Tarek",
            "surname_list": [{"surname": "Zorvane"}],
        },
        "birth_ref_index": 0 if birth else -1,
        "death_ref_index": -1,
        "event_ref_list": [{"ref": "e4"}] if birth else [],
    }
    fake_gramps.routes["/api/people/"].append(twin)
    fake_gramps.routes["/api/events/"].append(
        {"handle": "e4", "date": {"dateval": [1, 1, birth, False], "year": birth}}
    )
    fake_gramps.routes["/api/metadata/"]["object_counts"]["people"] = 4
    return take_snapshot(make_client(fake_gramps), TITLE)


def only(items, name):
    return [i for i in items if i.name == name]


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


def test_fuzzy_match_catches_inserted_vowels(snapshot):
    assert [
        p.gramps_id for p in name_candidates("Brannock Harrowmere", snapshot.people)
    ] == ["I0003"]
    assert name_candidates("Completely Different", snapshot.people) == []


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


def test_near_name_is_not_confirmed_standalone(items, snapshot):
    clusters = build_clusters(only(items, "Tariq Zorvane"), snapshot, set(), {}, ())
    assert tiers(clusters) == {"Tariq Zorvane": PROBABLE}


def test_near_name_is_confirmed_once_its_cluster_anchors(items, snapshot):
    clusters = build_clusters(items, snapshot, set(), {}, ())
    assert tiers(clusters) == {
        "Tariq Zorvane": CONFIRMED,
        "Elowen Quillon": LINKED,
        "Marden Quillon": NONE,
        "Odalys Harrowmere": NONE,
    }
    assert clusters[0].anchor.person.gramps_id == "I0002"
    confirmed = next(r for r in clusters[0].resolutions if r.tier == CONFIRMED)
    assert confirmed.person.gramps_id == "I0001"
    assert "anchored at I0002" in confirmed.reason


def test_accepting_a_match_anchors_the_cluster(items, fake_gramps):
    fake_gramps.routes["/api/citations/"] = []
    unlinked = take_snapshot(make_client(fake_gramps), TITLE)
    first = build_clusters(items, unlinked, set(), {}, ())
    assert tiers(first)["Tariq Zorvane"] == PROBABLE
    accepted = {only(items, "Tariq Zorvane")[0].key: "I0001"}
    second = build_clusters(items, unlinked, set(), accepted, ())
    assert tiers(second)["Tariq Zorvane"] == CONFIRMED
    assert tiers(second)["Elowen Quillon"] == CONFIRMED


def _entry(name, change="Individual updated", birth_year=None):
    return Entry(
        datetime(2026, 8, 25, 11, 0, 0), name, birth_year, None, change, "admin", "u"
    )


def test_placeholder_names_are_notices_not_work():
    entries = [_entry("?.0 ?", "Family added"), _entry("Tariq Zorvane")]
    assert [i.name for i in work_items(entries)] == ["Tariq Zorvane"]
    snapshot = Snapshot((), 0, {})
    assert [n.entry.name for n in build_notices(entries, snapshot)] == ["?.0 ?"]
    assert is_placeholder("?.0 ?") and not is_placeholder("Tariq Zorvane")


def test_two_of_his_entries_claiming_one_person_is_flagged():
    zorvane = GrampsPerson("h1", "I0001", "Tariq", "Zorvane", birth_year=1944)
    snapshot = Snapshot((zorvane,), 1, {})
    entries = [
        _entry("Tariq Zorvane", birth_year=1944),
        _entry("Tariq Vantel.0 Zorvane"),
    ]
    clusters = build_clusters(work_items(entries), snapshot, set(), {}, ())
    by_name = {r.item.name: r for c in clusters for r in c.resolutions}
    assert by_name["Tariq Zorvane"].tier == CONFIRMED
    assert by_name["Tariq Vantel.0 Zorvane"].tier == PROBABLE
    assert "I0001 is already matched to his 'Tariq Zorvane'" in (
        by_name["Tariq Vantel.0 Zorvane"].reason
    )
    assert "already matched" not in by_name["Tariq Zorvane"].reason


def test_negative_decision_is_not_reproposed(items, snapshot):
    tariq = only(items, "Tariq Zorvane")
    negatives = {(tariq[0].key, "I0001")}
    clusters = build_clusters(tariq, snapshot, negatives, {}, ())
    assert tiers(clusters) == {"Tariq Zorvane": NONE}
    assert needing_decision(clusters) == []


def test_several_candidates_are_ambiguous_never_forced(items, fake_gramps):
    crowded = with_twin(fake_gramps)
    clusters = build_clusters(only(items, "Tariq Zorvane"), crowded, set(), {}, ())
    assert tiers(clusters) == {"Tariq Zorvane": AMBIGUOUS}
    assert len(needing_decision(clusters)[0].candidates) == 2


def test_clusters_split_on_gap_and_priority_surnames_sort_first(feed_text, snapshot):
    everything = work_items(parse_feed(feed_text, "example"))
    clusters = build_clusters(everything, snapshot, set(), {}, ("Quillon",))
    assert len(clusters) == 3
    assert clusters[0].relevant
    assert not clusters[1].relevant
    assert {r.item.name for r in clusters[0].resolutions} >= {"Elowen Quillon"}


def test_pending_and_negatives_persist(items, tmp_path):
    item = only(items, "Tariq Zorvane")[0]
    save_pending(tmp_path / "pending.json", [item])
    assert load_pending(tmp_path / "pending.json") == [item]
    save_negatives(tmp_path / "negatives.json", {("ab cd", "I0009")})
    assert load_negatives(tmp_path / "negatives.json") == {("ab cd", "I0009")}


def test_pending_item_resurfaces_without_a_fresh_edit(items):
    tariq = only(items, "Tariq Zorvane")[0]
    pending = [replace(tariq, note="asked a cousin")]
    assert combine([], pending) == pending


def test_pending_item_merges_with_a_fresh_edit(items):
    tariq = only(items, "Tariq Zorvane")[0]
    earlier = replace(
        tariq,
        changes=((datetime(2026, 4, 1, 9, 30), "Family added"),),
        note="asked a cousin",
    )
    (merged,) = combine([tariq], [earlier])
    assert merged.note == "asked a cousin"
    assert [change for _, change in merged.changes] == [
        "Individual updated",
        "Family added",
    ]


def resolve_tariq(items, snapshot, anchor_items=None, **years):
    tariq = replace(only(items, "Tariq Zorvane")[0], **years)
    group = [tariq, *(anchor_items or [])]
    clusters = build_clusters(group, snapshot, set(), {}, ())
    return next(r for c in clusters for r in c.resolutions if r.item.key == tariq.key)


def test_agreeing_birth_year_confirms_a_name_match(items, snapshot):
    resolution = resolve_tariq(items, snapshot, birth_year=1945)
    assert resolution.tier == CONFIRMED
    assert resolution.person.gramps_id == "I0001"
    assert "birth year agrees (1945)" in resolution.reason


def test_a_year_two_apart_is_a_conflict(items, snapshot):
    assert resolve_tariq(items, snapshot, birth_year=1943).tier == CONFIRMED
    resolution = resolve_tariq(items, snapshot, birth_year=1946)
    assert resolution.tier == PROBABLE
    assert "birth 1946 vs 1944" in resolution.reason


def test_conflicting_year_stays_a_question_even_in_an_anchored_cluster(items, snapshot):
    elowen = only(items, "Elowen Quillon")
    resolution = resolve_tariq(items, snapshot, elowen, birth_year=1949)
    assert resolution.tier == PROBABLE
    assert "birth 1949 vs 1944" in resolution.reason


def test_a_death_year_the_tree_lacks_is_no_evidence(items, snapshot):
    resolution = resolve_tariq(items, snapshot, death_year=2011)
    assert resolution.tier == PROBABLE
    assert resolution.reason == "name match only"


def test_years_pick_one_of_two_name_matches(items, fake_gramps):
    crowded = with_twin(fake_gramps, birth=1950)
    resolution = resolve_tariq(items, crowded, birth_year=1944)
    assert resolution.tier == CONFIRMED
    assert resolution.person.gramps_id == "I0001"
    assert "other name matches do not agree" in resolution.reason


def test_years_drop_a_conflicting_candidate_but_stay_a_question(items, fake_gramps):
    crowded = with_twin(fake_gramps)
    resolution = resolve_tariq(items, crowded, birth_year=1950)
    assert resolution.tier == PROBABLE
    assert [p.gramps_id for p in resolution.candidates] == ["I0004"]


def test_two_agreeing_name_matches_stay_ambiguous(items, fake_gramps):
    crowded = with_twin(fake_gramps, birth=1944)
    resolution = resolve_tariq(items, crowded, birth_year=1944)
    assert resolution.tier == AMBIGUOUS
    assert len(resolution.candidates) == 2


def test_years_come_from_the_newest_row_that_has_them(feed_text):
    entries = parse_feed(feed_text, "example")
    marden = only(work_items(entries), "Marden Quillon")[0]
    assert (marden.birth_year, marden.death_year) == (1912, 1962)
    assert marden.lifespan == "1912 - 1962"


def test_picture_and_deleted_rows_are_notices_not_work(feed_text, snapshot):
    recent = since(parse_feed(feed_text, "example"), CURSOR)
    marden = only(work_items(recent), "Marden Quillon")[0]
    assert [change for _, change in marden.changes] == ["Individual added"]
    notices = build_notices(recent, snapshot)
    assert [(n.entry.name, n.entry.change) for n in notices] == [
        ("Marden Quillon", "Picture added"),
        ("Zenobe Vantel", "Individual deleted"),
    ]
    assert [n.person for n in notices] == [None, None]


def test_notice_names_the_linked_gramps_person(feed_text, snapshot):
    entries = parse_feed(feed_text, "example")
    elowen = replace(entries[0], name="Elowen Quillon")
    (notice,) = build_notices([elowen], snapshot)
    assert notice.person.gramps_id == "I0002"


def test_unrecognised_change_is_a_notice(feed_text, snapshot):
    entries = parse_feed(feed_text, "example")
    odd = replace(entries[0], change="Note modified")
    assert work_items([odd]) == []
    assert len(build_notices([odd], snapshot)) == 1
