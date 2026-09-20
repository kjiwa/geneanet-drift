from datetime import datetime

import pytest

from geneanet_drift.feed import (
    FeedError,
    format_lifespan,
    name_key,
    parse_feed,
    person_url,
    since,
)


@pytest.fixture
def entries(feed_text):
    return parse_feed(feed_text, "example")


def named(entries, name):
    return next(e for e in entries if e.name == name)


def test_record_count_and_ends(entries):
    assert len(entries) == 9
    assert (entries[0].when, entries[0].name, entries[0].change) == (
        datetime(2026, 8, 25, 16, 10, 3),
        "Marden Quillon",
        "Picture added",
    )
    assert (entries[-1].when, entries[-1].name) == (
        datetime(2023, 7, 15, 11, 0, 0),
        "Ilsabet Vantongeren",
    )


def test_row_without_lifespan_has_no_years(entries):
    tariq = named(entries, "Tariq Zorvane")
    assert (tariq.birth_year, tariq.death_year, tariq.admin) == (None, None, "adminone")


def test_lifespan_forms(entries):
    elowen = named(entries, "Elowen Quillon")
    assert (elowen.birth_year, elowen.death_year) == (1950, None)
    zenobe = named(entries, "Zenobe Vantel")
    assert (zenobe.birth_year, zenobe.death_year) == (None, 1968)
    marden = entries[0]
    assert (marden.birth_year, marden.death_year) == (1912, 1962)


def test_row_without_admin_keeps_its_change(entries):
    brannoch = named(entries, "Brannoch Harrowmere")
    assert (brannoch.change, brannoch.admin) == ("Individual updated", "")


def test_dotted_name_is_kept_as_displayed(entries):
    assert named(entries, "Kestrin.0 Vantel").change == "Individual updated"


def test_deletion_and_picture_rows_are_known_labels(entries):
    assert named(entries, "Zenobe Vantel").change == "Individual deleted"
    assert entries[0].change == "Picture added"
    assert all(e.known for e in entries)


def test_extra_blank_lines_inside_a_record_are_harmless(entries):
    odalys = named(entries, "Odalys Harrowmere")
    assert (odalys.birth_year, odalys.change, odalys.admin) == (
        1964,
        "Individual added",
        "adminone",
    )


def test_header_lines_are_ignored(feed_text):
    assert feed_text.startswith("Date\n\nIndividual\n\nUpdate\n\nAdmin\n")


def test_unknown_change_label_is_kept_and_flagged():
    text = "August 25, 2026\n\n10:00:00\n\nTariq Zorvane\n\nNote modified\n\nadminone"
    (entry,) = parse_feed(text, "example")
    assert (entry.change, entry.admin, entry.known) == (
        "Note modified",
        "adminone",
        False,
    )


def test_pagination_footer_is_cut(feed_text):
    footer = "\n\n1\n\n2\n\n3\n\n...\n\n12\n"
    assert len(parse_feed(feed_text + footer, "example")) == 9
    inline = feed_text.rstrip() + "\n\n1 2 3 ... 12\n"
    assert parse_feed(inline, "example")[-1].admin == "adminone"


def test_future_last_sync_filters_everything(entries):
    assert since(entries, datetime(2026, 8, 25, 16, 10, 3)) == []
    assert since(entries, datetime(2099, 1, 1)) == []


def test_last_sync_compares_times_not_just_dates(entries):
    midday = datetime(2026, 8, 25, 14, 30)
    assert [e.name for e in since(entries, midday)] == [
        "Marden Quillon",
        "Tariq Zorvane",
    ]
    assert len(since(entries, datetime(2026, 4, 2))) == 6
    assert len(since(entries, None)) == 9


def test_person_url_matches_observed_href_pattern(entries):
    assert (
        named(entries, "Tariq Zorvane").url
        == "https://gw.geneanet.org/example_w?n=zorvane&p=tariq&oc=0"
    )


def test_multi_word_given_name_uses_plus():
    assert person_url("example", "Ana Maria Quillon").endswith(
        "n=quillon&p=ana+maria&oc=0"
    )


def test_name_key_folds_case_and_whitespace_only():
    assert name_key("  Elowen   QUILLON ") == name_key("elowen quillon")
    assert name_key("Kestrin.0 Vantel") != name_key("Kestrin.1 Vantel")
    assert name_key("Tariq Zorvane") != name_key("Tarik Zorvane")


def test_format_lifespan():
    assert format_lifespan(1912, 1962) == "1912 - 1962"
    assert format_lifespan(1964, None) == "1964 -"
    assert format_lifespan(None, 1968) == "- 1968"
    assert format_lifespan(None, None) == ""


def test_text_without_records_is_rejected():
    with pytest.raises(FeedError, match="English"):
        parse_feed("Date\nIndividual\nnothing here", "example")


def test_date_without_a_time_names_its_line():
    with pytest.raises(FeedError, match="line 3: 'August 25, 2026' is not followed"):
        parse_feed("Date\n\nAugust 25, 2026\n\nTariq Zorvane", "example")


def test_record_with_a_stray_line_names_its_line(feed_text):
    broken = feed_text.replace(
        "adminone\n\nAugust 24, 2026\n\n17:20",
        "adminone\n\nstray\n\nmore\n\nAugust 24, 2026\n\n17:20",
        1,
    )
    with pytest.raises(FeedError, match=r"line \d+: expected a change line"):
        parse_feed(broken, "example")


def test_record_without_a_person_is_rejected():
    with pytest.raises(FeedError, match="line 1: record has no person"):
        parse_feed("August 25, 2026\n\n10:00:00\n", "example")


def test_impossible_date_is_rejected():
    with pytest.raises(FeedError, match="not a date"):
        parse_feed("Foo 25, 2026\n\n10:00:00\n\nTariq\n\nIndividual added", "example")


def test_reordered_rows_are_flagged(feed_text):
    blocks = feed_text.split("August 25, 2026")
    swapped = "August 25, 2026".join([blocks[0], blocks[2], blocks[1], *blocks[3:]])
    with pytest.raises(FeedError, match="expected newest first"):
        parse_feed(swapped, "example")


def test_only_the_first_offenders_are_listed():
    text = "\n".join(f"August {day}, 2026\n\nx" for day in range(1, 8))
    with pytest.raises(FeedError, match=r"\(and 4 more\)"):
        parse_feed(text, "example")
