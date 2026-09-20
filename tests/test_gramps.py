import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from geneanet_drift import gramps
from geneanet_drift.feed import name_key
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsError,
    take_snapshot,
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


def test_a_read_only_role_has_no_warnings(client):
    take_snapshot(client, TITLE)
    assert client.role_warnings() == []


def test_a_token_without_a_permissions_claim_is_an_error():
    broken = FakeGramps(token="not-a-jwt")
    with pytest.raises(GrampsError, match="no readable permissions claim"):
        take_snapshot(make_client(broken), TITLE)


def test_from_env_names_missing_variables():
    with pytest.raises(GrampsError, match="GRAMPS_USER and GRAMPS_PASSWORD"):
        GrampsClient.from_env(BASE_URL, {})


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


def test_unlinked_tree_has_no_links():
    client = make_client(FakeGramps(with_links=False))
    assert take_snapshot(client, TITLE).links == {}
