import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from geneanet_drift import gramps
from geneanet_drift.feed import name_key
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsDate,
    GrampsError,
    take_snapshot,
    take_tree,
    urllib_transport,
)
from tests.conftest import BASE_URL, FakeGramps, load_fixture, make_client, make_token

TITLE = "Geneanet - example family tree"


def test_snapshot_counts_people_and_reads_links(client):
    snapshot = take_snapshot(client, TITLE)
    assert [p.gramps_id for p in snapshot.people] == ["I0001", "I0002", "I0003"]
    assert snapshot.people[0].name == "Tarik Zorvane"
    assert snapshot.families == 1
    assert snapshot.links == {"elowen quillon": "I0002"}


def test_snapshot_reads_birth_and_death_years_from_events(client):
    tarik, elowen, brannoch = take_snapshot(client, TITLE).people
    assert (tarik.birth_year, tarik.death_year) == (1944, None)
    assert (elowen.birth_year, elowen.death_year) == (None, None)
    assert (brannoch.birth_year, brannoch.death_year) == (1971, None)


def test_only_the_named_source_supplies_links(client, fake_gramps):
    snapshot = take_snapshot(client, TITLE)
    assert "tarik zorvane" not in snapshot.links
    requested = [path for _, path in fake_gramps.requests]
    assert "/api/citations/c1" in requested
    assert "/api/citations/c2" not in requested
    assert "/api/citations/c3" not in requested


def test_occurrence_number_in_the_page_keeps_people_apart(fake_gramps):
    fake_gramps.routes["/api/people/"].append(
        {
            "handle": "h4",
            "gramps_id": "I0004",
            "primary_name": {
                "first_name": "Kestrin",
                "surname_list": [{"surname": "Vantel"}],
            },
        }
    )
    fake_gramps.routes["/api/metadata/"]["object_counts"]["people"] = 4
    fake_gramps.routes["/api/citations/"].append(
        {"handle": "c4", "source_handle": "s1", "page": "Kestrin.0 Vantel"}
    )
    fake_gramps.routes["/api/citations/c4"] = {
        "handle": "c4",
        "backlinks": {"person": ["h4"]},
    }
    links = take_snapshot(make_client(fake_gramps), TITLE).links
    assert links[name_key("Kestrin.0 Vantel")] == "I0004"
    assert name_key("Kestrin.1 Vantel") not in links


def test_only_the_token_request_is_a_post(client, fake_gramps):
    take_snapshot(client, TITLE)
    methods = [method for method, path in fake_gramps.requests if path != "/api/token/"]
    assert set(methods) == {"GET"}
    assert [m for m, _ in fake_gramps.requests].count("POST") == 1


def test_snapshot_rejects_a_people_count_mismatch(fake_gramps):
    fake_gramps.routes["/api/metadata/"] = {
        "object_counts": {"people": 4, "families": 1}
    }
    with pytest.raises(GrampsError, match="metadata reports 4"):
        take_snapshot(make_client(fake_gramps), TITLE)


def test_get_all_follows_pages(client, monkeypatch):
    monkeypatch.setattr(gramps, "PAGE_SIZE", 2)
    assert len(client.get_all("/api/people/")) == 3


def test_non_200_is_an_error(fake_gramps):
    def unauthorised(method, url, headers, body):
        return 401, {}, b"{}"

    with pytest.raises(GrampsError, match="HTTP 401"):
        GrampsClient(BASE_URL, "u", "p", transport=unauthorised).get("/api/metadata/")


def test_a_role_without_private_access_is_refused():
    guest = FakeGramps(token=make_token("EditOwnUser", "UseChat"))
    with pytest.raises(GrampsError, match="cannot view private records"):
        take_snapshot(make_client(guest), TITLE)


def test_a_writable_role_is_warned_about_but_allowed():
    editor = FakeGramps(token=make_token("ViewPrivate", "EditOwnUser", "AddObject"))
    client = make_client(editor)
    take_snapshot(client, TITLE)
    assert "AddObject" in client.role_warnings()[0]
    assert "EditOwnUser" not in client.role_warnings()[0]


class Expiring:
    """Rejects the first authorised GET with 401, like a token that just expired."""

    def __init__(self, permissions=("ViewPrivate",)):
        self.permissions = permissions
        self.mints = 0
        self.requests = 0
        self.expire_all = False

    def __call__(self, method, url, headers, body):
        if "/api/token/" in url:
            self.mints += 1
            token = make_token(*self.permissions) + str(self.mints)
            return 200, {}, json.dumps({"access_token": token}).encode()
        self.requests += 1
        stale = self.requests == 1 or self.expire_all
        if stale:
            return 401, {}, b"{}"
        return 200, {}, b'{"ok": true}'


def test_a_401_re_mints_the_token_once_and_retries_after_the_rate_limit(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(gramps.time, "sleep", sleeps.append)
    server = Expiring()
    client = GrampsClient(BASE_URL, "reader", "secret", transport=server)
    assert client.get("/api/metadata/")[0] == {"ok": True}
    assert server.mints == 2
    assert sleeps and sleeps[0] > 0


def test_a_second_401_is_an_error(monkeypatch):
    monkeypatch.setattr(gramps.time, "sleep", lambda seconds: None)
    server = Expiring()
    server.expire_all = True
    client = GrampsClient(BASE_URL, "reader", "secret", transport=server)
    with pytest.raises(GrampsError, match="HTTP 401"):
        client.get("/api/metadata/")
    assert server.mints == 2


def test_the_writer_also_refreshes_its_token(monkeypatch):
    monkeypatch.setattr(gramps.time, "sleep", lambda seconds: None)
    server = Expiring(permissions=("AddObject", "EditObject", "DeleteObject"))
    writer = gramps.GrampsWriter(BASE_URL, "editor", "secret", transport=server)
    assert writer.post_json("/api/objects/", []) == {"ok": True}
    assert server.mints == 2


def test_the_writer_needs_delete_for_undo_and_says_so():
    server = Expiring(permissions=("AddObject", "EditObject"))
    writer = gramps.GrampsWriter(BASE_URL, "editor", "secret", transport=server)
    with pytest.raises(GrampsError, match="DeleteObject.*undo"):
        writer.get("/api/metadata/")


def test_the_writer_allows_harmless_extra_permissions_but_not_unknown_deletes():
    base = ("AddObject", "EditObject", "DeleteObject", "ViewOtherUser", "UseChat")
    ok = Expiring(permissions=base)
    writer = gramps.GrampsWriter(BASE_URL, "editor", "secret", transport=ok)
    writer._mint_token()
    bad = Expiring(permissions=(*base, "DeleteUser"))
    writer = gramps.GrampsWriter(BASE_URL, "editor", "secret", transport=bad)
    with pytest.raises(GrampsError, match="DeleteUser"):
        writer._mint_token()


def test_a_read_only_role_has_no_warnings(client):
    take_snapshot(client, TITLE)
    assert client.role_warnings() == []


def test_a_token_without_a_permissions_claim_is_an_error():
    broken = FakeGramps(token="not-a-jwt")
    with pytest.raises(GrampsError, match="no readable permissions claim"):
        take_snapshot(make_client(broken), TITLE)


def test_urllib_transport_over_loopback():
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps(load_fixture("metadata.json")).encode()
            self.send_response(200)
            self.send_header("X-Total-Count", "7")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, payload = urllib_transport(
            "GET", f"http://127.0.0.1:{server.server_port}/api/metadata/", {}, None
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert headers["x-total-count"] == "7"
    assert json.loads(payload)["object_counts"]["people"] == 3
    assert seen == ["/api/metadata/"]


def test_an_unreachable_host_is_a_gramps_error(monkeypatch):
    def refuse(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gramps.urllib.request, "urlopen", refuse)
    with pytest.raises(GrampsError, match="cannot reach .*connection refused"):
        urllib_transport("GET", f"{BASE_URL}/api/metadata/", {}, None)


def test_unlinked_tree_has_no_links():
    client = make_client(FakeGramps(with_links=False))
    assert take_snapshot(client, TITLE).links == {}


def test_tree_joins_a_person_to_events_places_notes_citations_and_media(tree_client):
    tree = take_tree(tree_client)
    marenka = tree.people[0]
    assert (marenka.given, marenka.surname, marenka.nickname) == (
        "Marenka",
        "Thistledown",
        "Mari",
    )
    assert marenka.gender == "female"
    assert marenka.other_names == ("Marenka Brindle", "Renka")
    assert [e.kind for e in marenka.events] == ["Birth", "Death", "Baptism"]
    assert marenka.events[0].place == "Ardmoor, Fenwick, Eastmere"
    assert marenka.events[1].place is None
    assert marenka.events[2].description == "Parish register"
    assert [n.text for n in marenka.notes] == ["Moved to the coast."]
    assert (marenka.citations[0].source, marenka.citations[0].page) == (
        "Parish register of Ardmoor",
        "folio 12",
    )
    assert marenka.citations[0].date == GrampsDate(1921, None, None, None, None, "")
    assert [(m.description, m.path) for m in marenka.media] == [
        ("Wedding portrait", "portrait.jpg")
    ]


def test_event_notes_and_citations_are_read(tree_client):
    birth, death, _ = take_tree(tree_client).people[0].events
    assert [n.text for n in birth.notes] == ["Moved to the coast."]
    assert [c.page for c in birth.citations] == ["folio 12"]
    assert (death.notes, death.citations) == ((), ())


def test_a_place_cycle_does_not_loop():
    from geneanet_drift.gramps import _place_title

    places = {
        "a": {"name": {"value": "A"}, "placeref_list": [{"ref": "b"}]},
        "b": {"name": {"value": "B"}, "placeref_list": [{"ref": "a"}]},
    }
    assert _place_title("a", places) == "A, B"


def test_tree_dates_carry_precision_modifier_and_quality(tree_client):
    birth, death, baptism = take_tree(tree_client).people[0].events
    assert birth.date == GrampsDate(1920, 5, 3, None, None, "")
    assert death.date == GrampsDate(1990, None, None, "about", None, "")
    assert baptism.date == GrampsDate(1920, None, None, None, None, "")
    marriage = take_tree(tree_client).families[1].events[0]
    assert marriage.date == GrampsDate(1945, 6, None, "before", "estimated", "")


def test_tree_links_a_person_to_both_families_and_parents(tree_client):
    tree = take_tree(tree_client)
    marenka = tree.people[0]
    assert (marenka.parent_families, marenka.families) == (("f1",), ("f2", "f3"))
    parents, marriage, partnership = tree.families
    assert (parents.father, parents.mother, parents.children) == ("p2", "p3", ("p1",))
    assert (marriage.father, marriage.mother, marriage.kind) == ("p4", "p1", "Married")
    assert (partnership.father, partnership.kind) == (None, "Unknown")


def test_tree_reads_each_object_type_once_and_never_writes(tree_client, fake_gramps):
    take_tree(tree_client)
    paths = [path for method, path in fake_gramps.requests if method == "GET"]
    assert len(paths) == len(set(paths)) == 8
    assert [m for m, _ in fake_gramps.requests].count("POST") == 1


def test_an_undated_event_has_no_date():
    from geneanet_drift.gramps import _date

    assert _date({"dateval": [0, 0, 0, False], "modifier": 0, "quality": 0}) is None


def test_date_ranges_keep_their_end_year_and_short_datevals_do_not_crash():
    from geneanet_drift.gramps import _date

    span = _date({"dateval": [0, 0, 1900, False, 0, 0, 1910, False], "modifier": 4})
    assert (span.modifier, span.year, span.end_year) == ("range", 1900, 1910)
    assert _date({"dateval": [0, 1], "text": "x"}).month == 1


def test_date_ranges_keep_their_end_month_and_day():
    from geneanet_drift.gramps import _date

    span = _date({"dateval": [2, 3, 1900, False, 4, 5, 1910, False], "modifier": 4})
    assert (span.end_year, span.end_month, span.end_day) == (1910, 5, 4)
    plain = _date({"dateval": [2, 3, 1900, False], "modifier": 0})
    assert (plain.end_year, plain.end_month, plain.end_day) == (None, None, None)


def test_events_where_the_person_is_only_a_witness_are_not_theirs():
    from geneanet_drift.gramps import _events

    events = {"e1": object(), "e2": object()}
    refs = [
        {"ref": "e1", "role": "Primary"},
        {"ref": "e2", "role": {"string": "Witness"}},
    ]
    assert _events(refs, events) == (events["e1"],)


def test_place_title_skips_unnamed_places():
    from geneanet_drift.gramps import _place_title

    places = {
        "a": {"name": {"value": "A"}, "placeref_list": [{"ref": "b"}]},
        "b": {"name": {"value": ""}, "placeref_list": [{"ref": "c"}]},
        "c": {"name": {"value": "C"}},
    }
    assert _place_title("a", places) == "A, C"
