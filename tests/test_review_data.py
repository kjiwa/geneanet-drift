from geneanet_drift.decisions import ACCEPTED, REJECTED, Decision
from geneanet_drift.match_tree import CANDIDATES, LINKED
from geneanet_drift.review_data import (
    Review,
    accepted_changes,
    derive,
    field_changes,
    link_changes,
    people_view,
)
from tests.builders import (
    SOURCE,
    family,
    gramps,
    his,
    his_event,
    his_people,
    his_union,
    ref,
    tree,
)


def review(people, gramps_tree, negatives=frozenset()):
    return Review(his_people(*people), gramps_tree, SOURCE, frozenset(negatives), None)


def twin_review():
    mine = tree(
        [
            gramps("pa", "Hugh", "Ashby"),
            gramps("pb", "Hugh", "Ashby"),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
            gramps("kb", "Rowan", "Ashby", parent_families=("fb",)),
        ],
        [
            family("fa", father="pa", children=["ka"]),
            family("fb", father="pb", children=["kb"]),
        ],
    )
    people = [
        his(1, "Hugh", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    return review(people, mine)


def link_to(rev, person, gramps_id, decisions=None):
    found = [
        c
        for c in link_changes(rev, decisions or {})
        if c.person == person and c.field == f"link:{gramps_id}"
    ]
    assert len(found) == 1
    return found[0]


def test_choosing_a_parent_links_the_child_by_support():
    rev = twin_review()
    assert derive(rev, {}).matches[2].kind == CANDIDATES
    chosen = link_to(rev, 1, "I-pb")
    matches = derive(rev, {chosen.id: Decision(ACCEPTED)}).matches
    assert (matches[1].kind, matches[1].handle) == (LINKED, "pb")
    assert (matches[2].kind, matches[2].handle) == (LINKED, "kb")


def test_a_chosen_link_row_stays_so_the_choice_can_be_undone():
    rev = twin_review()
    chosen = link_to(rev, 1, "I-pb")
    decisions = {chosen.id: Decision(ACCEPTED)}
    ids = {c.id for c in link_changes(rev, decisions) if c.person == 1}
    assert chosen.id in ids and len(ids) == 2
    view = {p["key"]: p for p in people_view(rev, decisions)}
    assert view[1]["why"] == "chosen by you"
    assert view[1]["mine"]["id"] == "I-pb"
    assert len(view[1]["candidates"]) == 2


def test_rejecting_a_link_removes_the_candidate():
    rev = twin_review()
    rejected = link_to(rev, 1, "I-pa")
    decisions = {rejected.id: Decision(REJECTED)}
    matches = derive(rev, decisions).matches
    assert matches[1].candidates == ("pb",)
    assert not [c for c in link_changes(rev, decisions) if c.field == "link:I-pa"]


def test_a_choice_cascades_into_field_changes_and_accepted_changes():
    rev = twin_review()
    chosen = link_to(rev, 1, "I-pb")
    decisions = {chosen.id: Decision(ACCEPTED)}
    assert not [c for c in link_changes(rev, decisions) if c.person == 2]
    assert [c.id for c, _ in accepted_changes(rev, decisions)] == [chosen.id]


def test_without_decisions_the_ambiguous_pair_stays_unresolved():
    matches = derive(twin_review(), {}).matches
    assert (matches[1].kind, matches[2].kind) == (CANDIDATES, CANDIDATES)


def test_an_event_difference_of_a_cascaded_person_is_reported():
    mine = tree(
        [
            gramps("pa", "Hugh", "Ashby"),
            gramps("pb", "Hugh", "Ashby"),
            gramps("kb", "Rowan", "Ashby", parent_families=("fb",)),
        ],
        [family("fb", father="pb", children=["kb"])],
    )
    people = [
        his(1, "Hugh", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),), events=(his_event("born", 1850),)),
        his(9, "Mara", "Quill"),
    ]
    rev = review(people, mine)
    chosen = link_to(rev, 1, "I-pb")
    changes = field_changes(rev, {chosen.id: Decision(ACCEPTED)})
    assert any(c.person == 2 and c.field == "event:born" for c in changes)
