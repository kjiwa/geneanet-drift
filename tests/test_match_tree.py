from geneanet_drift.match_tree import (
    CANDIDATES,
    LINKED,
    NEW,
    REFUSED_WHY,
    match_people,
)
from tests.builders import (
    SOURCE,
    crosswalk,
    family,
    gramps,
    gramps_event,
    his,
    his_event,
    his_people,
    his_union,
    ref,
    tree,
)


def match(people, gramps_tree, negatives=frozenset(), refused=frozenset()):
    return match_people(
        his_people(*people), gramps_tree, SOURCE, set(negatives), refused=refused
    )


def test_citation_under_the_geneanet_source_links_by_display_name():
    result = match(
        [his(1, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby", citations=(crosswalk("rowan ashby"),))]),
    )
    assert result[1].kind == LINKED
    assert result[1].handle == "g1"


def test_citation_under_another_source_is_not_a_crosswalk():
    result = match(
        [his(1, "Rowan", "Ashby", events=(his_event("born", 1950),))],
        tree(
            [
                gramps(
                    "g1",
                    "Rowan",
                    "Ashby",
                    citations=(crosswalk("Rowan Ashby", "Other"),),
                ),
                gramps("g2", "Rowan", "Ashby"),
            ]
        ),
    )
    assert result[1].kind == CANDIDATES
    assert set(result[1].candidates) == {"g1", "g2"}


def test_crosswalk_page_on_two_gramps_people_links_neither():
    citation = (crosswalk("Rowan Ashby"),)
    result = match(
        [his(1, "Rowan", "Ashby")],
        tree(
            [
                gramps("g1", "Rowan", "Ashby", citations=citation),
                gramps("g2", "Rowan", "Ashby", citations=citation),
            ]
        ),
    )
    assert result[1].kind == CANDIDATES


def test_two_of_his_people_with_one_name_do_not_share_a_crosswalk():
    result = match(
        [his(1, "Rowan", "Ashby"), his(2, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))]),
    )
    assert {r.kind for r in result.values()} == {CANDIDATES}


def test_a_name_alone_never_links():
    result = match([his(1, "Rowan", "Ashby")], tree([gramps("g1", "Rowan", "Ashby")]))
    assert result[1].kind == CANDIDATES
    assert result[1].candidates == ("g1",)
    assert result[1].why == "name match only"


def test_name_and_agreeing_year_links():
    result = match(
        [his(1, "Rowan", "Ashby", events=(his_event("born", 1950),))],
        tree([gramps("g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1950),))]),
    )
    assert (result[1].kind, result[1].handle) == (LINKED, "g1")
    assert "birth year agrees" in result[1].why


def test_year_conflict_stays_a_candidate_with_the_reason():
    result = match(
        [his(1, "Rowan", "Ashby", events=(his_event("born", 1950),))],
        tree([gramps("g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1900),))]),
    )
    assert result[1].kind == CANDIDATES
    assert "years differ" in result[1].why


def test_year_agreement_narrows_two_name_matches_to_one():
    result = match(
        [his(1, "Rowan", "Ashby", events=(his_event("born", 1950),))],
        tree(
            [
                gramps("g1", "Rowan", "Ashby", events=(gramps_event("Birth", 1950),)),
                gramps("g2", "Rowan", "Ashby", events=(gramps_event("Birth", 1900),)),
            ]
        ),
    )
    assert (result[1].kind, result[1].handle) == (LINKED, "g1")


def test_no_name_match_is_new():
    result = match([his(1, "Rowan", "Ashby")], tree([gramps("g1", "Ivy", "Brook")]))
    assert result[1].kind == NEW


def test_a_negative_pair_removes_the_candidate():
    result = match(
        [his(1, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby")]),
        negatives={("rowan ashby", "I-g1")},
    )
    assert result[1].kind == NEW


def test_a_negative_pair_also_blocks_a_crosswalk():
    result = match(
        [his(1, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))]),
        negatives={("rowan ashby", "I-g1")},
    )
    assert result[1].kind == NEW


def test_a_gramps_person_linked_elsewhere_is_not_offered_again():
    result = match(
        [his(1, "Rowan", "Ashby"), his(2, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))]),
    )
    assert result[1].kind == result[2].kind == CANDIDATES


def two_fathers():
    return tree(
        [
            gramps("pa", "Edmund", "Ashby", citations=(crosswalk("Edmund Ashby"),)),
            gramps("pb", "Hugh", "Ashby", citations=(crosswalk("Hugh Ashby"),)),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
            gramps("kb", "Rowan", "Ashby", parent_families=("fb",)),
        ],
        [
            family("fa", father="pa", children=["ka"]),
            family("fb", father="pb", children=["kb"]),
        ],
    )


def father_and_son(father_name: tuple[str, str]):
    return [
        his(1, *father_name, unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]


def test_matched_parents_pick_between_equal_name_matches():
    result = match(father_and_son(("Hugh", "Ashby")), two_fathers())
    assert (result[2].kind, result[2].handle) == (LINKED, "kb")
    assert "relatives" in result[2].why


def test_a_candidate_with_a_contradicting_parent_loses():
    gramps_tree = tree(
        [
            gramps("pa", "Edmund", "Ashby", citations=(crosswalk("Edmund Ashby"),)),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
        ],
        [family("fa", father="pa", children=["ka"])],
    )
    people = [
        his(1, "Hugh", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    gramps_tree = tree(
        [
            *gramps_tree.people,
            gramps("pb", "Hugh", "Ashby", citations=(crosswalk("Hugh Ashby"),)),
        ],
        gramps_tree.families,
    )
    assert match(people, gramps_tree)[2].kind == NEW


def test_matches_propagate_down_a_chain_until_nothing_changes():
    gramps_tree = tree(
        [
            gramps(
                "a",
                "Edmund",
                "Ashby",
                events=(gramps_event("Birth", 1900),),
                families=("f1",),
            ),
            gramps("b", "Rowan", "Ashby", parent_families=("f1",), families=("f2",)),
            gramps("c", "Rowan", "Ashby", parent_families=("f2",)),
            gramps("d", "Rowan", "Ashby"),
        ],
        [
            family("f1", father="a", children=["b"]),
            family("f2", father="b", children=["c"]),
        ],
    )
    people = [
        his(
            1,
            "Edmund",
            "Ashby",
            events=(his_event("born", 1900),),
            unions=(his_union(1, 9, children=[2]),),
        ),
        his(
            2,
            "Rowan",
            "Ashby",
            parents=(ref(1),),
            unions=(his_union(2, 9, children=[3]),),
        ),
        his(3, "Rowan", "Ashby", parents=(ref(2),)),
        his(9, "Mara", "Quill"),
    ]
    result = match(people, gramps_tree)
    assert [(result[k].kind, result[k].handle) for k in (1, 2, 3)] == [
        (LINKED, "a"),
        (LINKED, "b"),
        (LINKED, "c"),
    ]


def test_two_people_proposing_one_gramps_person_both_stay_candidates():
    gramps_tree = tree(
        [
            gramps(
                "pa",
                "Edmund",
                "Ashby",
                citations=(crosswalk("Edmund Ashby"),),
                families=("fa",),
            ),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
        ],
        [family("fa", father="pa", children=["ka"])],
    )
    people = [
        his(1, "Edmund", "Ashby", unions=(his_union(1, 9, children=[2, 3]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(3, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    result = match(people, gramps_tree)
    assert result[2].kind == result[3].kind == CANDIDATES


def twin_fathers():
    return tree(
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


def chosen_match(people, gramps_tree, chosen):
    return match_people(his_people(*people), gramps_tree, SOURCE, set(), chosen)


def test_a_chosen_parent_links_the_child_by_support():
    people = father_and_son(("Hugh", "Ashby"))
    assert match(people, twin_fathers())[2].kind == CANDIDATES
    result = chosen_match(people, twin_fathers(), {1: "pb"})
    assert (result[1].kind, result[1].handle) == (LINKED, "pb")
    assert result[1].why == "chosen by you"
    assert (result[2].kind, result[2].handle) == (LINKED, "kb")


def test_a_chosen_person_keeps_its_pool_as_candidates():
    result = chosen_match(father_and_son(("Hugh", "Ashby")), twin_fathers(), {1: "pb"})
    assert set(result[1].candidates) == {"pa", "pb"}


def test_a_chosen_pair_beats_a_crosswalk_for_the_same_person():
    gramps_tree = tree(
        [
            gramps("g1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),)),
            gramps("g2", "Rowan", "Ashby"),
        ]
    )
    result = chosen_match([his(1, "Rowan", "Ashby")], gramps_tree, {1: "g2"})
    assert (result[1].kind, result[1].handle) == (LINKED, "g2")


def test_a_chosen_pair_beats_a_crosswalk_for_the_same_handle():
    gramps_tree = tree(
        [gramps("g1", "Rowan", "Ashby", citations=(crosswalk("Rowan Ashby"),))]
    )
    people = [his(1, "Rowan", "Ashby"), his(2, "Mara", "Quill")]
    result = chosen_match(people, gramps_tree, {2: "g1"})
    assert (result[2].kind, result[2].handle) == (LINKED, "g1")
    assert result[1].kind != LINKED


def father_by_child():
    return tree(
        [
            gramps("pa", "Edmund", "Vance"),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
        ],
        [family("fa", father="pa", children=["ka"])],
    )


def linked_child_people():
    return [
        his(1, "Hugh", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Rowan", "Ashby", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]


def test_a_relative_of_a_matched_person_is_a_candidate_without_a_name():
    result = chosen_match(linked_child_people(), father_by_child(), {2: "ka"})
    assert result[1].kind == CANDIDATES
    assert result[1].candidates == ("pa",)
    assert result[1].why == "relative of a matched person"


def test_a_relative_candidate_joins_the_name_candidates():
    gramps_tree = tree(
        [
            gramps("pa", "Edmund", "Vance"),
            gramps("pb", "Hugh", "Ashby"),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
        ],
        [family("fa", father="pa", children=["ka"])],
    )
    result = chosen_match(linked_child_people(), gramps_tree, {2: "ka"})
    assert set(result[1].candidates) == {"pa", "pb"}
    assert result[1].why == "name match only; relative of a matched person"


def test_a_taken_handle_is_not_a_relative_candidate():
    people = linked_child_people() + [his(3, "Edmund", "Vance")]
    result = chosen_match(people, father_by_child(), {2: "ka", 3: "pa"})
    assert result[1].kind == NEW


def test_a_negative_pair_is_not_a_relative_candidate():
    result = match_people(
        his_people(*linked_child_people()),
        father_by_child(),
        SOURCE,
        {("hugh ashby", "I-pa")},
        {2: "ka"},
    )
    assert result[1].kind == NEW


def test_a_relative_candidate_must_have_a_compatible_sex():
    gramps_tree = tree(
        [
            gramps("pa", "Edmund", "Vance", gender="female"),
            gramps("ka", "Rowan", "Ashby", parent_families=("fa",)),
        ],
        [family("fa", father="pa", children=["ka"])],
    )
    result = chosen_match(linked_child_people(), gramps_tree, {2: "ka"})
    assert result[1].kind == NEW


def namesake_trees():
    people = [his(1, "Wren", "Calloway"), his(2, "Wren", "Calloway")]
    mine = tree([gramps("w1", "Wren", "Calloway"), gramps("w2", "Wren", "Calloway")])
    return people, mine


def test_a_refused_person_is_new_with_no_candidates():
    people, mine = namesake_trees()
    result = match(people, mine, refused={1})
    assert (result[1].kind, result[1].why) == (NEW, REFUSED_WHY)
    assert result[1].candidates == ()


def test_a_refused_namesake_leaves_the_other_its_candidates():
    people, mine = namesake_trees()
    result = match(people, mine, refused={1})
    assert result[2].kind == CANDIDATES
    assert set(result[2].candidates) == {"w1", "w2"}


def test_a_refused_person_is_not_linked_by_a_crosswalk_citation():
    result = match(
        [his(1, "Rowan", "Ashby")],
        tree([gramps("g1", "Rowan", "Ashby", citations=(crosswalk("rowan ashby"),))]),
        refused={1},
    )
    assert result[1].kind == NEW


def test_a_refused_person_is_not_seeded_by_a_choice():
    people, mine = namesake_trees()
    result = match_people(
        his_people(*people), mine, SOURCE, set(), {1: "w1"}, frozenset({1})
    )
    assert result[1].kind == NEW


def test_a_refused_person_gets_no_relative_candidates():
    mine = tree(
        [
            gramps("rb", "Rowan", "Ashby", families=("fr",)),
            gramps("jk", "Jory", "Pell", parent_families=("fr",)),
        ],
        [family("fr", father="rb", children=["jk"])],
    )
    people = [
        his(1, "Rowan", "Ashby", unions=(his_union(1, 9, children=[2]),)),
        his(2, "Wren", "Vale", parents=(ref(1),)),
        his(9, "Mara", "Quill"),
    ]
    chosen = {1: "rb"}
    kept = match_people(his_people(*people), mine, SOURCE, set(), chosen)
    assert kept[2].candidates == ("jk",)
    refused = match_people(
        his_people(*people), mine, SOURCE, set(), chosen, frozenset({2})
    )
    assert (refused[2].kind, refused[2].candidates) == (NEW, ())
