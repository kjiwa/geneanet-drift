import shutil
from pathlib import Path

import pytest

from geneanet_drift.capture import (
    Address,
    CaptureError,
    Date,
    Event,
    Ref,
    load_tree,
    parse_person,
)

GENEANET = Path(__file__).parent / "fixtures" / "geneanet"
TREE = "exampletree"


def page(name: str) -> str:
    return (GENEANET / f"{name}.html").read_text(encoding="utf-8")


def parse(name: str):
    return parse_person(page(name), TREE)


def with_section(name: str, section: str) -> str:
    return page(name).replace("</body>", f"{section}\n</body>")


def test_public_name_layout_takes_the_name_from_the_links():
    person = parse("person_rich")
    assert (person.key, person.given, person.surname) == (6, "Rowan", "Ashby")
    assert person.public_name == "Rowan Ashby"
    assert person.sex == "male"
    assert person.address == Address("ashby", "rowan", 0)


def test_linked_name_layout_has_no_public_name():
    person = parse("person_two_unions")
    assert (person.given, person.surname, person.public_name) == (
        "Edmund",
        "Ashby",
        None,
    )


def test_events_occupation_and_derived_lines():
    person = parse("person_rich")
    assert person.occupation == "Carpenter"
    assert person.events == (
        Event("born", Date("April 11, 1950", 1950, 4, 11, None, None), "Lakeside"),
        Event("deceased", Date("March 2, 2019", 2019, 3, 2, None, None), "Harbortown"),
    )


def test_about_date_and_place_with_commas():
    born = parse("person_two_unions").events[0]
    assert born.date == Date("about 1892", 1892, None, None, "about", None)
    assert born.place == "Old Mill, Northshire, ENGLAND"


def test_parents_carry_addresses_and_a_nickname_is_ignored():
    person = parse("person_rich")
    assert [r.address for r in person.parents] == [
        Address("ashby", "edmund", 0),
        Address("brook", "ivy", 0),
    ]
    assert all(r.key is None for r in person.parents)


def test_union_with_date_place_and_children():
    (union,) = parse("person_rich").unions
    assert union.key == 1
    assert union.kind == "married"
    assert union.spouse == Ref(None, Address("quill", "nell", 0))
    assert union.date == Date("September 29, 1979", 1979, 9, 29, None, None)
    assert union.place == "Harbortown"
    assert union.children == (Ref(None, Address("ashby", "wren", 0)),)


def test_two_unions_without_marriage_details():
    first, second = parse("person_two_unions").unions
    assert (first.key, second.key) == (2, 3)
    assert (first.date, first.place, second.date) == (None, None, None)
    assert len(first.children) == 2
    assert [c.address.given for c in second.children] == ["dora", "kit"]


def test_nickname_after_a_spouse_is_not_a_second_spouse():
    (union,) = parse("person_source").unions
    assert union.spouse.address == Address("lowe", "arthur", 0)
    assert union.place == "Westbury UK"
    assert union.children == ()


def test_notes_and_sources():
    person = parse("person_rich")
    assert [(n.kind, n.text) for n in person.notes] == [
        ("Individual Note", "Built boats.\nLived above the harbor.")
    ]
    (source,) = parse("person_source").sources
    assert (source.fact, source.text) == ("Death", "telephone call from a cousin")


def test_media_come_from_the_page_configuration_object():
    (item,) = parse("person_rich").media
    assert (item.id, item.title, item.is_default) == (1001, "Harbor, 1975", True)
    assert item.date is None
    assert item.original.endswith("normal.jpg?t=1")
    assert item.thumbnail.endswith("medium.jpg?t=1")
    assert parse("person_sparse").media == ()


def test_sparse_person_has_only_a_name_and_parents():
    person = parse("person_sparse")
    assert (person.key, person.given, person.surname) == (408, "Pip", "Marsh")
    assert person.events == ()
    assert len(person.parents) == 2
    assert person.occupation is None


def test_occurrence_number_is_part_of_the_address():
    html = page("person_occurrence").replace(
        "</title>",
        '</title><link rel="canonical" href="https://gw.geneanet.org/exampletree'
        '?lang=en&amp;n=quill&amp;oc=1&amp;p=sam">',
    )
    person = parse_person(html, TREE)
    assert person.address == Address("quill", "sam", 1)
    assert person.parents[0].address == Address("quill", "sam", 0)
    assert person.events[0].date == Date("in 1970", 1970, None, None, None, None)


def test_page_without_canonical_link_is_rejected():
    with pytest.raises(CaptureError, match="canonical"):
        parse("person_occurrence")


@pytest.fixture
def shapes():
    return parse("person_shapes")


def test_date_forms(shapes):
    born, deceased, buried, killed = shapes.events
    assert born.date == Date("possibly in 1850", 1850, None, None, None, "estimated")
    assert deceased.date == Date("before March 3, 1921", 1921, 3, 3, "before", None)
    assert buried.date == Date("in March, 1921", 1921, 3, None, None, None)
    assert killed == Event("killed", None, None)


def test_place_may_contain_hyphens(shapes):
    assert shapes.events[0].place == "Old Mill, Northshire, ENGLAND"
    assert shapes.events[2].place == "Elm Hill - East Section"


def test_header_lines_other_than_events(shapes):
    assert shapes.occupation == "Lamplighter"
    assert shapes.other_names == ("Tam",)
    assert shapes.public_name is None


def test_relationship_and_placeholder_spouse(shapes):
    relation, placeholder, place_only = shapes.unions
    assert relation.kind == "relationship"
    assert relation.spouse.address == Address("reed", "bram", 0)
    assert placeholder.spouse == Ref(9, None, placeholder=True)
    assert placeholder.date == Date(
        "about September, 1870", 1870, 9, None, "about", None
    )
    assert placeholder.place == "Harbortown"
    assert (place_only.date, place_only.place) == (None, "Millbrook")


def test_derived_sections_are_skipped(shapes):
    assert [r.address.given for r in shapes.parents if r.address] == ["ada"]
    assert shapes.sources == ()


def test_family_note_attaches_to_the_only_union_and_surname_may_be_empty():
    person = parse("person_empty_surname")
    assert (person.given, person.surname, person.sex) == ("Cy", "", "unknown")
    assert person.address == Address("", "cy", 0)
    (union,) = person.unions
    assert [(n.kind, n.text) for n in union.notes] == [
        ("Union", "Met at the harbor.\nWed in spring.")
    ]


def test_family_note_on_a_page_with_several_unions_is_rejected():
    html = with_section(
        "person_shapes",
        '<h2><span>Family Note</span></h2><div class="fiche-note-union">x</div>',
    )
    with pytest.raises(CaptureError, match="3 unions"):
        parse_person(html, TREE)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda h: h.replace("<span>Parents</span>", "<span>Cousins</span>"),
        lambda h: h.replace("Born possibly in&nbsp;1850", "Born 1850"),
        lambda h: h.replace("Born possibly in&nbsp;1850", "Born in&nbsp;May 3, 1850"),
        lambda h: h.replace("Born possibly in&nbsp;1850", "Born since 1850"),
        lambda h: h.replace("Lamplighter", "Lamplighter</li><li>Chandler"),
        lambda h: h.replace("Relationship with", "Engaged to"),
        lambda h: h.replace('alt="Female"', 'alt="Other"', 1),
        lambda h: h.replace("m=MOD_IND&amp;i=20", "m=MOD_IND"),
    ],
    ids=[
        "section",
        "bare-year",
        "in-with-day",
        "date-word",
        "second-free-line",
        "union-text",
        "sex",
        "key",
    ],
)
def test_unknown_shapes_fail_loudly(mutation):
    with pytest.raises(CaptureError):
        parse_person(mutation(page("person_shapes")), TREE)


@pytest.fixture
def pages(tmp_path):
    shutil.copytree(GENEANET / "tree", tmp_path / "pages")
    return tmp_path / "pages"


def test_load_tree_resolves_keys_addresses_and_shared_addresses(pages):
    people = load_tree(pages, TREE)
    assert sorted(people) == [1, 2, 3, 4, 5]
    (ada_union,) = people[1].unions
    assert ada_union.spouse.key == 2
    assert [c.key for c in ada_union.children] == [3]
    assert people[2].unions[0].spouse.key == 1
    assert [r.key for r in people[3].parents] == [1, 2]


def test_load_tree_keeps_placeholder_keys_without_a_page(pages):
    people = load_tree(pages, TREE)
    assert people[4].parents == (Ref(9, None, placeholder=True),)
    assert people[5].unions[0].spouse.key == 9


def test_shared_address_without_a_link_back_is_unresolved(pages):
    orphan = (pages / "person-3.html").read_text().replace("p=ada", "p=eve")
    (pages / "person-3.html").write_text(orphan)
    with pytest.raises(CaptureError, match="person-1"):
        load_tree(pages, TREE)


def test_missing_key_that_is_not_a_placeholder_is_unresolved(pages):
    (pages / "person-2.html").unlink()
    with pytest.raises(CaptureError, match="person-1"):
        load_tree(pages, TREE)


def test_unreadable_page_is_reported_with_its_file(pages):
    broken = (pages / "person-5.html").read_text().replace("Relationship with", "Hm")
    (pages / "person-5.html").write_text(broken)
    with pytest.raises(CaptureError, match=r"person-5\.html"):
        load_tree(pages, TREE)


def test_malformed_media_item_is_reported_with_its_file(pages):
    text = (pages / "person-1.html").read_text()
    broken = text.replace(
        "</body>", '<script>var c = {"media":[{"doc_id":1}]};</script></body>'
    )
    assert broken != text
    (pages / "person-1.html").write_text(broken)
    with pytest.raises(CaptureError, match=r"person-1\.html"):
        load_tree(pages, TREE)


def test_files_that_are_not_numbered_pages_are_ignored(pages):
    (pages / "person-1-copy.html").write_text("not a page")
    assert 1 in load_tree(pages, TREE)


def test_duplicate_media_are_listed_once():
    html = page("person_rich")
    script = next(s for s in html.split("<script>")[1:] if "doc_images" in s)
    doubled = html.replace(
        "</body>", "<script>" + script.split("</script>")[0] + "</script></body>"
    )
    assert doubled != html
    assert len(parse_person(doubled, TREE).media) == len(parse_person(html, TREE).media)
