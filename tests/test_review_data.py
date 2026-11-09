from geneanet_drift.capture import Address, Media, Note
from geneanet_drift.decisions import ACCEPTED, REJECTED, Decision
from geneanet_drift.gramps import GrampsMedia
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
    crosswalk,
    family,
    gramps,
    his,
    his_event,
    his_people,
    his_union,
    ref,
    tree,
)


def review(people, gramps_tree, negatives=frozenset(), gramps_url="http://gramps.test"):
    return Review(
        his_people(*people),
        gramps_tree,
        SOURCE,
        frozenset(negatives),
        None,
        "example",
        gramps_url,
    )


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


def waiting_review():
    mine = tree(
        [
            gramps("pa", "Hugh", "Ashby"),
            gramps("pb", "Hugh", "Ashby"),
            gramps(
                "kb",
                "Rowan",
                "Ashby",
                parent_families=("fb",),
                citations=(crosswalk("Rowan Ashby"),),
            ),
        ],
        [family("fb", father="pb", children=["kb"])],
    )
    people = [
        his(1, "Hugh", "Ashby"),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
    ]
    return review(people, mine)


def rows_of(rev, key, decisions=None):
    return {
        r["label"]: r for r in people_view(rev, decisions or {})[key - 1]["changes"]
    }


def test_a_waiting_row_is_informational_and_names_the_person_to_match():
    row = rows_of(waiting_review(), 2)["Parent"]
    assert row["kind"] == "differs"
    assert row["informational"] is True
    assert (row["waiting_on"], row["waiting_name"]) == (1, "Hugh Ashby")
    assert row["his"] == ["Hugh Ashby (no match yet)"]
    assert row["mine"] == ["Hugh Ashby"]


def test_a_row_that_waits_on_nobody_has_no_waiting_fields():
    rows = rows_of(twin_review(), 1)
    assert all(
        r["waiting_on"] is None and r["waiting_name"] is None for r in rows.values()
    )


def test_relatives_that_will_be_created_are_marked_new_in_parent_and_union_rows():
    mine = tree([gramps("k", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))])
    people = [
        his(
            2,
            "Rowan",
            "Ashby",
            parents=(ref(1),),
            unions=(his_union(5, 3, children=[4]),),
        ),
        his(1, "Hugh", "Ashby"),
        his(3, "Nell", "Quill", sex="female"),
        his(4, "Wren", "Ashby"),
    ]
    rows = rows_of(review(people, mine), 2)
    assert rows["Parent"]["his"] == ["Hugh Ashby (new)"]
    assert rows["Marriage or partnership"]["his"] == [
        "Spouse: Nell Quill (new)",
        "Children: Wren Ashby (new)",
    ]
    assert rows["Parent"]["informational"] is False


def relative_review():
    mine = tree(
        [
            gramps(
                "rb",
                "Rowan",
                "Ashby",
                families=("fr",),
                citations=(crosswalk("Rowan Ashby"),),
            ),
            gramps("jk", "Jory", "Pell", parent_families=("fr",)),
        ],
        [family("fr", father="rb", children=["jk"])],
    )
    people = [
        his(1, "Rowan", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Wren", "Vale", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    return review(people, mine)


def test_a_person_chosen_through_a_relative_keeps_the_choice_as_a_candidate():
    rev = relative_review()
    offered = link_to(rev, 2, "I-jk")
    decisions = {offered.id: Decision(ACCEPTED)}
    match = derive(rev, decisions).matches[2]
    assert (match.kind, match.handle, match.candidates) == (LINKED, "jk", ("jk",))
    assert link_to(rev, 2, "I-jk", decisions).id == offered.id
    assert people_view(rev, decisions)[1]["mine"]["id"] == "I-jk"


def test_conflicting_accepted_links_for_one_person_settle_on_the_first():
    rev = twin_review()
    first, second = (c for c in link_changes(rev, {}) if c.person == 1)
    decisions = {first.id: Decision(ACCEPTED), second.id: Decision(ACCEPTED)}
    matches = derive(rev, decisions).matches
    assert matches[1].handle == first.proposed["handle"]


def three_candidate_review():
    mine = tree(
        [
            gramps("pa", "Hugh", "Ashby"),
            gramps("pb", "Hugh", "Ashby"),
            gramps("pc", "Hugh", "Ashby"),
            gramps("mm", "Ione", "Quill", gender="unknown"),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
            gramps("kb", "Rowan", "Ashby", parent_families=("fb",)),
            gramps("kc", "Rowan", "Ashby", parent_families=("fc",)),
        ],
        [
            family("fa", father="pa", children=["ka"]),
            family("fb", father="pb", mother="mm", children=["kb"]),
            family("fc", father="pc", children=["kc"]),
        ],
    )
    people = [
        his(1, "Hugh", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    return review(people, mine)


def test_choosing_among_three_candidates_keeps_the_list_at_three():
    rev = three_candidate_review()
    before = derive(rev, {}).matches[1].candidates
    chosen = link_to(rev, 1, "I-pb")
    after = derive(rev, {chosen.id: Decision(ACCEPTED)}).matches[1]
    assert len(before) == 3
    assert after.handle == "pb"
    assert after.candidates == before
    rows = link_changes(rev, {chosen.id: Decision(ACCEPTED)})
    assert len([c for c in rows if c.person == 1]) == 3


def new_person_lines(rev, key=2):
    person = next(p for p in people_view(rev, {}) if p["key"] == key)
    return next(r["his"] for r in person["changes"] if r["label"] == "New person")


def test_a_new_person_lists_linked_and_new_parents_with_their_match_state():
    mine = tree([gramps("hb", "Hugh", "Ashby", citations=(crosswalk("Hugh Ashby"),))])
    people = [
        his(1, "Hugh", "Ashby"),
        his(2, "Rowan", "Ashby", parents=(ref(1), ref(3), ref(99))),
        his(3, "Ida", "Quill", sex="female"),
    ]
    lines = new_person_lines(review(people, mine))
    assert "Parent: Hugh Ashby (in Gramps: I-hb)" in lines
    assert "Parent: Ida Quill (new)" in lines
    assert not any("recorded" in line or "99" in line for line in lines)


def test_a_new_person_lists_the_spouse_and_children_of_a_union():
    mine = tree([gramps("k", "Mara", "Quill", citations=(crosswalk("Mara Quill"),))])
    people = [
        his(2, "Rowan", "Ashby", unions=(his_union(5, 3, children=[4, 98]),)),
        his(3, "Mara", "Quill", sex="female"),
        his(4, "Wren", "Ashby"),
    ]
    lines = new_person_lines(review(people, mine))
    assert "Spouse: Mara Quill (in Gramps: I-k)" in lines
    assert "Children: Wren Ashby (new)" in lines


def test_notes_and_photos_of_a_new_person_stay_counts():
    people = [his(2, "Rowan", "Ashby", notes=(Note("general", "Farmer"),))]
    assert "1 note recorded" in new_person_lines(review(people, tree([])))


def media_row(held):
    person = his(
        1, "Rowan", "Ashby", media=(Media(1, "Harvest", None, "o", "t", False),)
    )
    mine = tree(
        [
            gramps(
                "g1",
                "Rowan",
                "Ashby",
                media=held,
                citations=(crosswalk("Rowan Ashby"),),
            )
        ]
    )
    return rows_of(review([person], mine), 1)["Photo or document"]


def test_a_photo_row_lists_the_photos_gramps_already_holds():
    held = (
        GrampsMedia("m1", "Wedding portrait", "a.jpg", "image/jpeg"),
        GrampsMedia("m2", "", "scans/letter.png", "image/png"),
    )
    row = media_row(held)
    assert row["mine"] == ["Already in Gramps: Wedding portrait, letter.png"]
    assert row["his"] == ["Harvest"]


def test_a_photo_row_says_when_gramps_holds_none():
    assert media_row(())["mine"] == ["No photos in Gramps."]


ADDRESS = Address("ashby", "rowan", 0)


def link_view(gramps_url, address=ADDRESS):
    mine = tree([gramps("h1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))])
    rev = review(
        [his(1, "Rowan", "Ashby", address=address)], mine, gramps_url=gramps_url
    )
    return people_view(rev, {})[0]


def test_person_links_point_at_both_trees():
    person = link_view("http://gramps.test/")
    assert person["his_url"].endswith("/example_w?n=ashby&p=rowan&oc=0")
    assert person["mine"]["url"] == "http://gramps.test/person/I-h1"


def test_links_are_omitted_without_an_address_or_gramps_url():
    person = link_view("", address=None)
    assert person["his_url"] is None
    assert person["mine"]["url"] is None


def test_candidates_link_to_their_gramps_pages():
    candidates = people_view(twin_review(), {})[0]["candidates"]
    assert {c["mine_url"] for c in candidates} == {
        "http://gramps.test/person/I-pa",
        "http://gramps.test/person/I-pb",
    }
