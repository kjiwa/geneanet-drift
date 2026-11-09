from geneanet_drift.capture import Date, Event, Media, Note, Source
from geneanet_drift.diff import (
    DIFFERS,
    MISSING_IN_HIS,
    NEW_PERSON,
    NEW_VALUE,
    change_id,
    diff_people,
)
from geneanet_drift.gramps import GrampsCitation, GrampsEvent, GrampsMedia, GrampsNote
from geneanet_drift.match_tree import CANDIDATES, LINKED, NEW, Match, match_people
from tests.builders import (
    SOURCE,
    crosswalk,
    family,
    gramps,
    gramps_date,
    gramps_event,
    his,
    his_date,
    his_event,
    his_people,
    his_union,
    ref,
    tree,
)

LINK = Match(LINKED, "g1")


def diff_one(person, mine, extra_people=(), extra_mine=(), families=(), matches=None):
    people = his_people(person, *extra_people)
    gramps_tree = tree([mine, *extra_mine], families)
    matches = matches or {k: Match(NEW) for k in people} | {person.key: LINK}
    return diff_people(people, gramps_tree, matches)


def only(changes, field):
    (found,) = [c for c in changes if c.field == field]
    return found


def fields(changes):
    return {(c.field, c.kind) for c in changes}


def base():
    return his(1, "Rowan", "Ashby"), gramps("g1", "Rowan", "Ashby")


def test_an_identical_person_has_no_changes():
    person, mine = base()
    assert diff_one(person, mine) == []


def test_names_and_gender_report_all_three_kinds():
    person = his(1, "Rowan", "Ashby", sex="female")
    mine = gramps("g1", "Rowen", "", gender="unknown")
    changes = diff_one(person, mine)
    assert only(changes, "given").kind == DIFFERS
    assert only(changes, "given").proposed == "Rowan"
    assert only(changes, "surname").kind == NEW_VALUE
    assert only(changes, "gender").kind == NEW_VALUE
    only_mine = diff_one(
        his(1, "Rowan", "", sex="unknown"), gramps("g1", "Rowan", "Ashby")
    )
    assert only(only_mine, "surname").kind == MISSING_IN_HIS
    assert only(only_mine, "surname").proposed is None


def test_event_kinds_new_differs_and_missing():
    person = his(
        1,
        "Rowan",
        "Ashby",
        events=(his_event("born", 1950), his_event("deceased", 2019)),
    )
    mine = gramps(
        "g1",
        "Rowan",
        "Ashby",
        events=(gramps_event("Death", 2018), gramps_event("Burial", 2019)),
    )
    changes = diff_one(person, mine)
    assert fields(changes) == {
        ("event:born", NEW_VALUE),
        ("event:deceased", DIFFERS),
        ("event:buried", MISSING_IN_HIS),
    }
    assert only(changes, "event:deceased").proposed["date"]["year"] == 2019


def test_killed_events_are_compared_like_the_others():
    person = his(1, "Rowan", "Ashby", events=(his_event("killed", 1944),))
    mine = gramps("g1", "Rowan", "Ashby")
    assert fields(diff_one(person, mine)) == {("event:killed", NEW_VALUE)}


def test_place_difference_keeps_the_agreeing_date():
    person = his(1, "Rowan", "Ashby", events=(his_event("born", 1950, "Lakeside"),))
    mine = gramps(
        "g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1950, "Harbortown"),)
    )
    change = only(diff_one(person, mine), "event:born")
    assert change.kind == DIFFERS
    assert change.proposed["place"] == "Lakeside"


def test_a_place_that_his_side_only_abbreviates_changes_nothing():
    person = his(
        1, "Rowan", "Ashby", events=(his_event("born", 1950, "Harbor-Town, Utopia"),)
    )
    mine = gramps(
        "g1",
        "Rowan",
        "Ashby",
        events=(
            gramps_event("Birth", 1950, "Harbor Town Region, Old Quarter, Utopia"),
        ),
    )
    assert diff_one(person, mine) == []


def test_event_with_equal_facts_and_a_blank_his_place_is_unchanged():
    person = his(1, "Rowan", "Ashby", events=(his_event("born", 1950),))
    mine = gramps(
        "g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1950, "Harbortown"),)
    )
    assert diff_one(person, mine) == []


def test_a_less_precise_date_on_his_side_changes_nothing():
    coarse = diff_one(
        his(1, "Rowan", "Ashby", events=(Event("born", his_date(1950), None),)),
        gramps(
            "g1",
            "Rowan",
            "Ashby",
            events=(GrampsEvent("e", "Birth", gramps_date(1950, 5, 3), None, ""),),
        ),
    )
    assert coarse == []


def test_a_less_precise_date_on_my_side_proposes_his():
    exact = Date("May 3, 1950", 1950, 5, 3, None, None)
    fine = diff_one(
        his(1, "Rowan", "Ashby", events=(Event("born", exact, None),)),
        gramps("g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1950),)),
    )
    assert only(fine, "event:born").kind == DIFFERS
    assert only(fine, "event:born").proposed["date"]["day"] == 3


def test_an_event_with_no_date_and_no_place_changes_nothing():
    person = his(1, "Rowan", "Ashby", events=(his_event("deceased"),))
    assert diff_one(person, gramps("g1", "Rowan", "Ashby")) == []
    bundle = diff_people(his_people(person), tree([]), {1: Match(NEW)})[0]
    assert bundle.proposed["events"] == {}


def test_a_less_precise_union_date_changes_nothing():
    union = his_union(7, 2, date=his_date(1979))
    marriage = GrampsEvent("m", "Marriage", gramps_date(1979, 6, 1), None, "")
    assert union_case(union, [family("f1", "g1", "g2", events=(marriage,))]) == []


def test_two_unions_with_one_spouse_claim_the_family_once():
    person = his(
        1,
        "Rowan",
        "Ashby",
        unions=(his_union(7, 2, children=[3]), his_union(8, 2, children=[3])),
    )
    mine = gramps("g1", "Rowan", "Ashby", families=("f1",))
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Nell", "Quill"), his(3, "Wren", "Ashby")),
        extra_mine=(gramps("g2", "Nell", "Quill"),),
        families=[family("f1", "g1", "g2")],
        matches={1: LINK, 2: Match(LINKED, "g2"), 3: Match(NEW)},
    )
    unions = [c for c in changes if c.person == 1]
    assert [(c.field, c.kind) for c in unions] == [
        ("union:7", DIFFERS),
        ("union:8", NEW_VALUE),
    ]


def test_occupation_comes_from_the_occupation_event_description():
    person = his(1, "Rowan", "Ashby", occupation="Carpenter")
    mine = gramps(
        "g1",
        "Rowan",
        "Ashby",
        events=(gramps_event("Occupation", description="Joiner"),),
    )
    change = only(diff_one(person, mine), "occupation")
    assert (change.kind, change.his, change.mine) == (DIFFERS, "Carpenter", "Joiner")


def test_other_names_notes_media_and_sources_report_only_what_is_absent():
    person = his(
        1,
        "Rowan",
        "Ashby",
        other_names=("Ro", "Rowan Ashby"),
        notes=(
            Note("Individual Note", "Moved  to the coast."),
            Note("Individual Note", "New fact"),
        ),
        media=(
            Media(1, "Wedding portrait", None, "o1", "t1", False),
            Media(2, "Harvest", "1950", "o2", "t2", False),
        ),
        sources=(Source("Birth", "Parish register"), Source("Death", "Gazette")),
    )
    mine = gramps(
        "g1",
        "Rowan",
        "Ashby",
        notes=(GrampsNote("n", "Moved to the coast. Later."),),
        media=(GrampsMedia("m", "wedding portrait", "p.jpg", "image/jpeg"),),
        citations=(GrampsCitation("c", "Church book", "Parish register p. 2", None),),
    )
    changes = diff_one(person, mine)
    assert [(c.field, c.kind) for c in changes] == [
        ("other_name", NEW_VALUE),
        ("note", NEW_VALUE),
        ("media", NEW_VALUE),
        ("source", NEW_VALUE),
    ]
    assert changes[0].his == "Ro"
    assert changes[1].his["text"] == "New fact"
    assert changes[2].his["title"] == "Harvest"
    assert changes[3].his["text"] == "Gazette"


def test_contact_notes_are_flagged_sensitive():
    person = his(
        1,
        "Rowan",
        "Ashby",
        notes=(Note("Contact", "call 555"), Note("Individual Note", "plain")),
    )
    changes = [
        c
        for c in diff_one(*(person, gramps("g1", "Rowan", "Ashby")))
        if c.field == "note"
    ]
    assert [c.sensitive for c in changes] == [True, False]


def test_parents_new_linked_new_unlinked_and_missing():
    person = his(1, "Rowan", "Ashby", parents=(ref(2), ref(3)))
    mine = gramps("g1", "Rowan", "Ashby", parent_families=("f1",))
    parent = gramps("g2", "Edmund", "Ashby")
    stranger = gramps("g9", "Hugh", "Brook")
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Edmund", "Ashby"), his(3, "Ivy", "Brook")),
        extra_mine=(parent, stranger),
        families=(family("f1", father="g9", mother=None),),
        matches={1: LINK, 2: Match(LINKED, "g2"), 3: Match(NEW)},
    )
    mine_changes = [c for c in changes if c.person == 1]
    assert {(c.field, c.kind) for c in mine_changes} == {
        ("parent:2", NEW_VALUE),
        ("parent:3", NEW_VALUE),
        ("parent:I-g9", MISSING_IN_HIS),
    }
    assert only(mine_changes, "parent:2").proposed == {"handle": "g2"}
    assert only(mine_changes, "parent:3").proposed == {"key": 3}


def test_an_unresolved_parent_paired_by_candidates_is_one_waiting_row():
    person = his(1, "Rowan", "Ashby", parents=(ref(2),))
    mine = gramps("g1", "Rowan", "Ashby", parent_families=("f1",))
    parent = gramps("g9", "Jiwa Bhim Karim", "Saleh")
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Jiwa", "Bhimji"),),
        extra_mine=(parent,),
        families=(family("f1", father="g9", mother=None),),
        matches={1: LINK, 2: Match(CANDIDATES, candidates=("g9",))},
    )
    (row,) = [c for c in changes if c.person == 1]
    assert (row.field, row.kind) == ("parent:2", DIFFERS)
    assert row.his == "Jiwa Bhimji"
    assert row.mine == "Jiwa Bhim Karim Saleh"
    assert row.proposed is None
    assert row.waiting_on == 2
    assert row.id == change_id(1, "parent:2", "Jiwa Bhimji")


def test_a_parent_whose_candidates_are_not_his_linked_parents_stays_new():
    person = his(1, "Rowan", "Ashby", parents=(ref(2),))
    mine = gramps("g1", "Rowan", "Ashby", parent_families=("f1",))
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Ivy", "Brook"),),
        extra_mine=(gramps("g9", "Hugh", "Brook"), gramps("g8", "Ivy", "Brook")),
        families=(family("f1", father="g9", mother=None),),
        matches={1: LINK, 2: Match(CANDIDATES, candidates=("g8",))},
    )
    assert fields([c for c in changes if c.person == 1]) == {
        ("parent:2", NEW_VALUE),
        ("parent:I-g9", MISSING_IN_HIS),
    }
    assert all(c.waiting_on is None for c in changes)


def test_an_unresolved_spouse_paired_by_candidates_is_one_waiting_union():
    person = his(1, "Rowan", "Ashby", unions=(his_union(7, 2, children=[3]),))
    mine = gramps("g1", "Rowan", "Ashby", families=("f1",))
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Nell", "Quill"), his(3, "Wren", "Ashby")),
        extra_mine=(gramps("g2", "Nel", "Quil"),),
        families=(family("f1", "g1", "g2"),),
        matches={
            1: LINK,
            2: Match(CANDIDATES, candidates=("g2",)),
            3: Match(NEW),
        },
    )
    (row,) = [c for c in changes if c.person == 1]
    assert (row.field, row.kind) == ("union:7", DIFFERS)
    assert row.mine["spouse"] == "Nel Quil"
    assert row.proposed is None
    assert row.waiting_on == 2


def test_a_child_awaiting_a_match_does_not_make_the_union_differ():
    kid = gramps("g5", "Ada", "Ashby")
    union = his_union(7, 2, children=[3])
    person = his(1, "Rowan", "Ashby", unions=(union,))
    mine = gramps("g1", "Rowan", "Ashby", families=("f1",))
    people = (his(2, "Nell", "Quill"), his(3, "Wren", "Ashby"))
    spouse = gramps("g2", "Nell", "Quill")
    fam = (family("f1", "g1", "g2", children=["g5"]),)

    def run(child_match):
        matches = {1: LINK, 2: Match(LINKED, "g2"), 3: child_match}
        changes = diff_one(person, mine, people, (spouse, kid), fam, matches)
        return [c for c in changes if c.person == 1]

    assert run(Match(CANDIDATES, candidates=("g5",))) == []
    assert only(run(Match(NEW)), "union:7").kind == DIFFERS
    stranger = Match(CANDIDATES, candidates=("g2",))
    assert only(run(stranger), "union:7").kind == DIFFERS


def union_case(his_union_, mine_families, extra_mine=()):
    person = his(1, "Rowan", "Ashby", unions=(his_union_,) if his_union_ else ())
    mine = gramps(
        "g1", "Rowan", "Ashby", families=tuple(f.handle for f in mine_families)
    )
    spouse = gramps("g2", "Nell", "Quill")
    changes = diff_one(
        person,
        mine,
        extra_people=(his(2, "Nell", "Quill"), his(3, "Wren", "Ashby")),
        extra_mine=(spouse, *extra_mine),
        families=mine_families,
        matches={1: LINK, 2: Match(LINKED, "g2"), 3: Match(NEW)},
    )
    return [c for c in changes if c.person == 1]


def test_union_without_a_matching_family_is_new():
    changes = [
        c for c in union_case(his_union(7, 2, children=[3]), []) if c.field == "union:7"
    ]
    (change,) = changes
    assert change.kind == NEW_VALUE
    assert change.proposed["spouse"] == {"handle": "g2"}
    assert change.proposed["children"] == [{"key": 3}]


def test_union_differs_when_a_child_is_missing_and_keeps_mine():
    kid = gramps("g5", "Ada", "Ashby")
    union = his_union(7, 2, children=[3], date=his_date(1979), place="Harbortown")
    changes = union_case(union, [family("f1", "g1", "g2", children=["g5"])], [kid])
    change = only(changes, "union:7")
    assert change.kind == DIFFERS
    assert change.proposed["children"] == [{"key": 3}, {"handle": "g5"}]
    assert change.proposed["place"] == "Harbortown"
    assert change.mine["children"] == ["Ada Ashby"]


def test_matching_union_is_unchanged_and_an_unlisted_family_is_missing_in_his():
    union = his_union(7, 2, date=his_date(1979))
    marriage = gramps_event("Marriage", 1979)
    assert union_case(union, [family("f1", "g1", "g2", events=(marriage,))]) == []
    changes = union_case(None, [family("f1", "g1", "g2")])
    assert fields(changes) == {("union:F-f1", MISSING_IN_HIS)}


def test_unmatched_person_becomes_one_new_person_bundle():
    person = his(
        1,
        "Rowan",
        "Ashby",
        events=(his_event("born", 1950, "Lakeside"),),
        notes=(Note("Contact", "x"),),
        unions=(his_union(7, 2),),
    )
    changes = diff_people(
        his_people(person, his(2, "Nell", "Quill")),
        tree([]),
        {1: Match(NEW), 2: Match(NEW)},
    )
    bundle = only([c for c in changes if c.person == 1], "person")
    assert bundle.kind == NEW_PERSON
    assert bundle.proposed["events"]["born"]["place"] == "Lakeside"
    assert bundle.proposed["unions"][0]["spouse"] == {"key": 2}
    assert bundle.sensitive


def test_candidates_yield_one_link_change_each():
    person = his(1, "Rowan", "Ashby")
    changes = diff_one(
        person,
        gramps("g1", "Rowan", "Ashby"),
        extra_mine=(gramps("g2", "Rowan", "Ashby"),),
        matches={1: Match(CANDIDATES, candidates=("g1", "g2"))},
    )
    assert [c.field for c in changes] == ["link:I-g1", "link:I-g2"]
    assert changes[0].proposed == {"handle": "g1"}
    assert changes[0].his == "Rowan Ashby"


def rich_case():
    people = his_people(
        his(
            1,
            "Rowan",
            "Ashby",
            events=(his_event("born", 1950),),
            occupation="Carpenter",
            unions=(his_union(7, 2, children=[3]),),
        ),
        his(2, "Nell", "Quill"),
        his(3, "Wren", "Ashby", parents=(ref(1), ref(2))),
        his(4, "Ivy", "Brook"),
    )
    gramps_tree = tree(
        [
            gramps("g1", "Rowen", "Ashby", citations=(crosswalk("Rowan Ashby"),)),
            gramps("g2", "Nell", "Quill", citations=(crosswalk("Nell Quill"),)),
        ]
    )
    return people, gramps_tree


def run(people, gramps_tree):
    matches = match_people(people, gramps_tree, SOURCE, set())
    return diff_people(people, gramps_tree, matches)


def test_ids_are_stable_across_runs_and_unique():
    first = run(*rich_case())
    second = run(*rich_case())
    assert [c.id for c in first] == [c.id for c in second]
    assert len({c.id for c in first}) == len(first) > 3
    assert {c.kind for c in first} >= {NEW_VALUE, DIFFERS, NEW_PERSON}


def test_id_survives_an_edit_to_my_side_but_not_to_his():
    people, gramps_tree = rich_case()
    before = only(run(people, gramps_tree), "given")
    edited = tree(
        [
            gramps("g1", "Rowin", "Ashby", citations=(crosswalk("Rowan Ashby"),)),
            *gramps_tree.people[1:],
        ]
    )
    after = only(run(people, edited), "given")
    assert before.mine != after.mine
    assert before.id == after.id
    renamed = {**people, 1: his(1, "Rowann", "Ashby", public_name="Rowan Ashby")}
    assert only(run(renamed, gramps_tree), "given").id != before.id
