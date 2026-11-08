import os
import shutil
from dataclasses import replace
from datetime import date, datetime

import pytest

from geneanet_drift.capture import Address, page_times, parse_person
from geneanet_drift.feed import Entry, FeedError
from geneanet_drift.status import entry_address
from geneanet_drift.wizard import capture_status, main
from tests.conftest import FIXTURES

TREE = "exampletree"
ROW = """<gw-history-editing-entry><div class="row entry">
<div><b>{day}</b><div>{time}</div></div>
<div><p><a href="/exampletree_w?n={n}&amp;p={p}&amp;oc=0">{name}</a></p></div>
<div><div>{change}</div><div>admin</div></div></div></gw-history-editing-entry>
"""
ADA = ("October 28, 2026", "20:37:23", "quill", "ada", "Ada Quill")
BRAM = ("October 20, 2026", "09:00:00", "reed", "bram", "Bram Reed")
ZED = ("October 10, 2026", "08:00:00", "absent", "zed", "Zed Absent")
OLD = ("March 3, 2026", "08:00:00", "vale", "dee", "Dee Vale")


def history_page(*rows) -> str:
    body = "".join(
        ROW.format(day=d, time=t, n=n, p=p, name=name, change="Individual updated")
        for d, t, n, p, name in rows
    )
    return f"<html><body>{body}</body></html>"


def stamp(path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


@pytest.fixture
def pages(tmp_path):
    shutil.copytree(FIXTURES / "geneanet" / "tree", tmp_path / "pages")
    return tmp_path / "pages"


def write_history(pages, *rows) -> None:
    (pages / "history-editing.html").write_text(history_page(*rows))


@pytest.fixture
def tcfg(cfg):
    return replace(cfg, tree=TREE)


def report(cfg, pages, feed=None, since=None) -> tuple[int, list[str]]:
    said: list[str] = []
    return capture_status(cfg, pages, feed, since, said.append), said


def test_page_times_map_addresses_to_file_mtimes(pages):
    stamp(pages / "person-1.html", datetime(2026, 10, 1, 9, 0, 0))
    times = page_times(pages, TREE)
    assert times[Address("quill", "ada", 0)] == datetime(2026, 10, 1, 9, 0, 0)
    assert Address("reed", "bram", 0) in times


def test_visit_list_has_missing_and_older_pages_with_urls(tcfg, pages):
    stamp(pages / "person-1.html", datetime(2026, 10, 1, 9, 0, 0))
    stamp(pages / "person-2.html", datetime(2026, 10, 25, 9, 0, 0))
    write_history(pages, ADA, BRAM, ZED, OLD)
    code, said = report(tcfg, pages)
    text = "\n".join(said)
    assert code == 0
    assert "Ada Quill" in text
    assert "https://gw.geneanet.org/exampletree_w?n=quill&p=ada&oc=0" in text
    assert "Zed Absent" in text and "not captured" in text
    assert "Bram Reed" not in text
    assert "Dee Vale" not in text
    assert "saved 2026-10-01 09:00:00" in text


def test_since_is_the_manual_cutoff(tcfg, pages):
    stamp(pages / "person-1.html", datetime(2026, 10, 1))
    write_history(pages, ADA, ZED)
    _, said = report(tcfg, pages, since=date(2026, 10, 15))
    text = "\n".join(said)
    assert "Ada Quill" in text and "Zed Absent" not in text
    assert "from 2026-10-15" in text
    _, said = report(tcfg, pages)
    assert "Zed Absent" in "\n".join(said)


def test_header_states_the_timezone_caveat(tcfg, pages):
    write_history(pages, ADA)
    _, said = report(tcfg, pages)
    text = " ".join(" ".join(said).split())
    assert "as Geneanet wrote them" in text
    assert "local file times" in text
    assert "few hours" in text


def test_nothing_to_visit_still_prints_the_next_commands(tcfg, pages):
    stamp(pages / "person-1.html", datetime(2026, 11, 1))
    write_history(pages, ADA)
    code, said = report(tcfg, pages)
    text = "\n".join(said)
    assert code == 0
    assert "Nothing to visit" in text
    for command in ("snapshot", "review", "apply"):
        assert f"geneanet-drift {command}" in text


def test_pasted_feed_is_the_fallback_when_no_history_page(cfg, pages, feed_text):
    feed = pages.parent / "feed.txt"
    feed.write_text(feed_text)
    code, said = report(cfg, pages, feed=feed)
    text = "\n".join(said)
    assert code == 0
    assert "Tariq Zorvane" in text
    assert "https://gw.geneanet.org/example_w?n=zorvane&p=tariq&oc=0" in text


def test_missing_history_and_feed_says_how_to_get_one(cfg, pages):
    with pytest.raises(FeedError, match="history/editing.*--feed"):
        report(cfg, pages)


def test_output_lines_fit_a_narrow_pane_apart_from_urls(tcfg, pages):
    write_history(pages, ADA, BRAM, ZED)
    _, said = report(tcfg, pages)
    lines = [ln for line in said for ln in line.splitlines()]
    unwrappable = ("    http", f"  {pages}")
    assert [ln for ln in lines if len(ln) > 40 and not ln.startswith(unwrappable)] == []
    assert all(ln.isascii() for ln in lines)


def test_no_subcommand_runs_capture_status_on_the_default_pages(
    cfg, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    default = home / "Downloads" / "geneanet-drift" / "example" / "pages"
    default.mkdir(parents=True)
    (default / "history-editing.html").write_text(
        history_page(
            ("October 28, 2026", "20:37:23", "quill", "ada", "Ada Quill")
        ).replace("exampletree_w", "example_w")
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert main(["--data-dir", str(cfg.data_dir)]) == 0
    assert "Ada Quill" in capsys.readouterr().out


def entry(url="", address=None) -> Entry:
    return Entry(datetime(2026, 10, 1), "A B", None, None, "x", "", url, address)


def test_entry_address_reuses_the_row_address_when_present():
    assert entry(address=("quill", "ada", "2")).url == ""
    assert entry_address(entry(address=("quill", "ada", "2"))) == Address(
        "quill", "ada", 2
    )


def test_entry_address_falls_back_to_the_url():
    url = "https://gw.geneanet.org/exampletree_w?n=quill&p=ada&oc=1"
    assert entry_address(entry(url)) == Address("quill", "ada", 1)
    assert entry_address(entry("")) is None


@pytest.mark.parametrize(
    "bad",
    [entry(address=("quill", "ada", "x")), entry("https://h/t_w?n=q&p=a&oc=x")],
)
def test_entry_address_with_a_non_numeric_oc_is_a_feed_error(bad):
    with pytest.raises(FeedError, match="occurrence"):
        entry_address(bad)


def test_canonical_address_read_cheaply_matches_the_full_parse(pages):
    for path in pages.glob("person-*.html"):
        expected = parse_person(path.read_text(encoding="utf-8"), TREE).address
        stamp(path, datetime(2026, 10, 1))
        assert expected in page_times(pages, TREE)


def test_an_unreadable_page_is_skipped_with_a_note(pages):
    broken = pages / "person-1.html"
    broken.chmod(0)
    notes: list[str] = []
    try:
        times = page_times(pages, TREE, notes.append)
    finally:
        broken.chmod(0o644)
    assert Address("quill", "ada", 0) not in times
    assert Address("reed", "bram", 0) in times
    assert len(notes) == 1 and "person-1.html" in notes[0]
