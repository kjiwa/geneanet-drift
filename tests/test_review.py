from dataclasses import replace
from datetime import date, datetime

import pytest

from geneanet_drift.config import end_of_day
from geneanet_drift.feed import parse_feed, since
from geneanet_drift.gramps import take_snapshot
from geneanet_drift.match import build_clusters, build_notices, work_items
from geneanet_drift.review import render, unticked

TITLE = "Geneanet - example family tree"
TODAY = date(2026, 9, 20)
CURSOR = end_of_day(date(2026, 4, 2))


@pytest.fixture
def sheet(client, feed_text):
    snapshot = take_snapshot(client, TITLE)
    recent = since(parse_feed(feed_text, "example"), CURSOR)
    clusters = build_clusters(work_items(recent), snapshot, set(), {}, ())
    notices = build_notices(recent, snapshot)
    return render("example", clusters, notices, [], TODAY, CURSOR)


def entry(sheet: str, title: str) -> str:
    return next(block for block in sheet.split("\n\n") if block.startswith(title))


def test_heading_names_anchor_and_counts(sheet):
    assert "## Cluster 1 - anchored at I0002 Elowen Quillon: 2 new, 2 updates" in sheet


def test_new_person_block_carries_the_gramps_ready_citation(sheet):
    block = entry(sheet, "[ ] Add person Marden Quillon")
    assert 'page = "Marden Quillon"   date = 2026-09-20 (accessed)' in block
    assert "Note:     (Person Note)" in block
    assert "confidence" not in block


def test_citation_recipe_is_stated_once(sheet):
    assert sheet.count(f'cites Source "{TITLE}"') == 1
    assert sheet.count("confidence = Low") == 1
    assert sheet.count("attribute URL = copy the address from the address bar") == 1
    assert "attribute URL = http" not in sheet


def test_stale_item_renders_no_compare_url(client, feed_text):
    snapshot = take_snapshot(client, TITLE)
    everything = parse_feed(feed_text, "example")
    clusters = build_clusters(work_items(everything), snapshot, set(), {}, ())
    text = render("example", clusters, [], [], TODAY, CURSOR)
    stale = [line for line in text.splitlines() if "no link in his change log" in line]
    assert len(stale) == 1
    assert stale[0].startswith("    Compare: ")
    assert "&p=kestrin" not in text


def test_confirmed_match_names_the_person_and_reason(sheet):
    block = entry(sheet, "[ ] Update Tarik Zorvane [I0001]")
    assert "cluster anchored at I0002" in block
    assert (
        "Compare: https://gw.geneanet.org/example_w?n=zorvane&p=tariq&oc=0&lang=en"
        in block
    )


def test_linked_person_gets_no_duplicate_citation(sheet):
    block = entry(sheet, "[ ] Check Elowen Quillon [I0002]")
    assert "Citation" not in block


def test_unticked_counts_only_checkbox_lines(sheet):
    assert unticked(sheet) == 4


def test_header_states_the_cursor(sheet):
    assert "Changes after: 2026-04-02 23:59:59" in sheet


def test_picture_and_deletion_rows_become_informational_lines(sheet):
    section = sheet.split("## Informational (nothing to tick)")[1]
    assert (
        "- 2026-08-25  a picture was added to Marden Quillon; "
        "view it on his page if you want the image" in section
    )
    assert (
        "- 2026-08-24  he deleted Zenobe Vantel; check whether yours should follow"
        in section
    )
    assert "[ ]" not in section


def test_unrecognised_change_is_called_out(client, feed_text):
    snapshot = take_snapshot(client, TITLE)
    entries = parse_feed(feed_text, "example")
    notices = build_notices([replace(entries[0], change="Note modified")], snapshot)
    text = render("example", [], notices, [], TODAY, datetime(2026, 4, 2))
    assert 'unrecognised change "Note modified" on Marden Quillon' in text


def test_placeholder_person_is_not_called_an_unrecognised_change(client, feed_text):
    snapshot = take_snapshot(client, TITLE)
    entries = parse_feed(feed_text, "example")
    placeholder = replace(entries[0], name="?.0 ?", change="Family added")
    text = render(
        "example",
        [],
        build_notices([placeholder], snapshot),
        [],
        TODAY,
        datetime(2026, 4, 2),
    )
    assert "on an unknown person" in text
    assert "unrecognised" not in text
