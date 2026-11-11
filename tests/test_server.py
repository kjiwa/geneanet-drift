import http.client
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from geneanet_drift.capture import Address, Media, Note
from geneanet_drift.config import Config, ConfigError
from geneanet_drift.decisions import (
    ACCEPTED,
    EDITED,
    NONE_PREFIX,
    REJECTED,
    Decision,
    load_decisions,
    save_decisions,
)
from geneanet_drift.gramps import GrampsMedia, GrampsPlace
from geneanet_drift.review_data import Review, apply_command, derive, people_view
from geneanet_drift.server import (
    TOKEN_HEADER,
    WEB_DIR,
    App,
    RequestError,
    make_server,
    serve,
)
from geneanet_drift.wizard import review_tree
from tests.builders import (
    SOURCE,
    crosswalk,
    gramps,
    gramps_event,
    his,
    his_event,
    his_people,
    tree,
)
from tests.test_review_data import relative_review, waiting_review

REPO = Path(__file__).parent.parent


def synthetic_review(
    media_dir: Path | None, thumbnails_dir: Path | None = None
) -> Review:
    people = his_people(
        his(
            1,
            "Rowan",
            "Ashby",
            events=(his_event("born", 1850, "Lyon"),),
            occupation="Miller",
            notes=(Note("Contact", "call 555-0100"),),
            media=(Media(7, "Harvest", None, "o", "t", False),),
            address=Address("ashby", "rowan", 0),
        ),
        his(2, "Ivy", "Marlowe", sex="female", events=(his_event("born", 1900),)),
        his(3, "Wren", "Calloway"),
        his(4, "Sage", "Harlow"),
    )
    mine = tree(
        [
            gramps(
                "h1",
                "Rowan",
                "Ashby",
                citations=(crosswalk("Rowan Ashby"),),
                events=(gramps_event("Birth", 1851),),
            ),
            gramps("w1", "Wren", "Calloway"),
            gramps("w2", "Wren", "Calloway"),
            gramps("h4", "Sage", "Harlow", citations=(crosswalk("Sage Harlow"),)),
        ],
        places=[GrampsPlace("pl1", "Lyon, Rhone, France")],
    )
    return Review(
        people,
        mine,
        SOURCE,
        frozenset(),
        media_dir,
        thumbnails_dir,
        "example",
        "http://gramps.test/",
        Path("pages"),
    )


class Client:
    def __init__(self, server, token: str):
        self.port = server.server_address[1]
        self.token = token

    def request(self, method, path, body=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        sent = dict(headers or {})
        if token:
            sent[TOKEN_HEADER] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        connection.request(method, path, data, sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response, raw

    def json(self, method, path, body=None, **kwargs):
        response, raw = self.request(method, path, body, **kwargs)
        return response.status, json.loads(raw)


@pytest.fixture
def media_dir(tmp_path):
    folder = tmp_path / "media" / "person-1"
    folder.mkdir(parents=True)
    (folder / "7-Harvest.jpg").write_bytes(b"\xff\xd8photo")
    (tmp_path / "media" / "secret.txt").write_text("private")
    return tmp_path / "media"


@pytest.fixture
def served(tmp_path, media_dir):
    cfg = Config("example", "http://gramps.test", tmp_path / "data", tmp_path)
    cfg.data_dir.mkdir()
    server = make_server(cfg, synthetic_review(media_dir), 0)
    app = server.RequestHandlerClass.app
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield Client(server, app.token), cfg, server
    server.shutdown()
    server.server_close()


def people_by_name(client) -> dict:
    status, payload = client.json("GET", "/api/changes")
    assert status == 200
    return {p["name"]: p for p in payload["people"]}


def test_binds_to_loopback_only(served):
    _, _, server = served
    assert server.server_address[0] == "127.0.0.1"


def test_changes_are_grouped_by_state(served):
    client, _, _ = served
    people = people_by_name(client)
    assert {n: p["state"] for n, p in people.items()} == {
        "Rowan Ashby": "differs",
        "Ivy Marlowe": "new",
        "Wren Calloway": "needs_match",
        "Sage Harlow": "unchanged",
    }
    _, payload = client.json("GET", "/api/changes")
    assert payload["summary"] == {
        "people": 4,
        "needs_match": 1,
        "new": 1,
        "differs": 1,
        "unchanged": 1,
        "accepted": 0,
    }
    assert len(people["Wren Calloway"]["candidates"]) == 2


def test_people_and_candidates_carry_links_to_both_trees(served):
    client, _, _ = served
    people = people_by_name(client)
    rowan = people["Rowan Ashby"]
    assert rowan["his_url"].endswith("/example_w?n=ashby&p=rowan&oc=0")
    assert rowan["mine"]["url"] == "http://gramps.test/person/I-h1"
    assert people["Ivy Marlowe"]["his_url"] is None
    urls = {c["mine_url"] for c in people["Wren Calloway"]["candidates"]}
    assert urls == {"http://gramps.test/person/I-w1", "http://gramps.test/person/I-w2"}


def test_rows_carry_both_sides_and_flags(served):
    client, _, _ = served
    rows = {r["label"]: r for r in people_by_name(client)["Rowan Ashby"]["changes"]}
    assert rows["Birth"]["his"] == ["1850, Lyon"]
    assert rows["Birth"]["mine"] == ["1851"]
    assert rows["Birth"]["editable"] == "fact"
    assert rows["Note"]["sensitive"] is True
    assert rows["Photo or document"]["photo"] == "/media/1/7-Harvest.jpg"


def test_photo_is_omitted_without_local_capture(tmp_path):
    review = synthetic_review(None)
    app = App(review, tmp_path / "decisions.json")
    rows = next(p for p in app.changes()["people"] if p["key"] == 1)["changes"]
    assert [r["photo"] for r in rows if r["label"] == "Photo or document"] == [None]


def test_api_requires_the_token(served):
    client, cfg, _ = served
    for method, path, body in (
        ("GET", "/api/changes", None),
        ("GET", "/api/preview", None),
        ("GET", "/api/places", None),
        ("POST", "/api/decisions", {"id": "x", "status": "accepted"}),
        ("POST", "/api/link", {"person": 3, "change": None}),
    ):
        status, _ = client.json(method, path, body, token=False)
        assert status == 403
    status, _ = client.json(
        "GET", "/api/changes", headers={TOKEN_HEADER: "wrong"}, token=False
    )
    assert status == 403
    assert not cfg.decisions_path.exists()


def test_host_header_must_be_loopback(served):
    client, _, _ = served
    status, _ = client.json("GET", "/api/changes", headers={"Host": "evil.test"})
    assert status == 403
    status, _ = client.json("GET", "/api/places", headers={"Host": "evil.test"})
    assert status == 403
    response, _ = client.request("GET", "/", headers={"Host": "evil.test:80"})
    assert response.status == 403


def test_post_must_be_json(served):
    client, _, _ = served
    response, _ = client.request(
        "POST", "/api/decisions", None, headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415


def first_row(client, label="Birth") -> dict:
    rows = people_by_name(client)["Rowan Ashby"]["changes"]
    return next(r for r in rows if r["label"] == label)


def test_decisions_persist_and_survive_a_restart(served):
    client, cfg, _ = served
    row = first_row(client)
    assert (
        client.json("POST", "/api/decisions", {"id": row["id"], "status": "accepted"})[
            0
        ]
        == 200
    )
    assert load_decisions(cfg.decisions_path) == {row["id"]: Decision(ACCEPTED)}
    assert first_row(client)["decision"] == {"status": "accepted", "value": None}
    assert client.json("GET", "/api/changes")[1]["summary"]["accepted"] == 1
    client.json("POST", "/api/decisions", {"id": row["id"], "status": "rejected"})
    assert load_decisions(cfg.decisions_path)[row["id"]] == Decision(REJECTED)
    client.json("POST", "/api/decisions", {"id": row["id"], "status": "pending"})
    assert load_decisions(cfg.decisions_path) == {}


def test_edit_is_validated_against_the_field(served):
    client, cfg, _ = served
    birth = first_row(client)
    value = {"date_text": "about 1850", "place": "Lyon"}
    status, _ = client.json(
        "POST",
        "/api/decisions",
        {"id": birth["id"], "status": "edited", "value": value},
    )
    assert status == 200
    assert load_decisions(cfg.decisions_path)[birth["id"]] == Decision(EDITED, value)
    assert first_row(client)["edit_lines"] == ["about 1850, Lyon"]
    for bad in (
        None,
        "text",
        {"date_text": "", "place": ""},
        {"date_text": 5, "place": ""},
    ):
        status, _ = client.json(
            "POST",
            "/api/decisions",
            {"id": birth["id"], "status": "edited", "value": bad},
        )
        assert status == 400
    photo = first_row(client, "Photo or document")
    status, _ = client.json(
        "POST", "/api/decisions", {"id": photo["id"], "status": "edited", "value": "x"}
    )
    assert status == 400


@pytest.mark.parametrize(
    "body",
    [
        {"id": "nope", "status": "accepted"},
        {"id": ["list"], "status": "accepted"},
        {"status": "accepted"},
    ],
)
def test_unknown_change_is_refused(served, body):
    client, cfg, _ = served
    assert client.json("POST", "/api/decisions", body)[0] == 404
    assert not cfg.decisions_path.exists()


def test_bad_status_is_refused(served):
    client, _, _ = served
    row = first_row(client)
    status, _ = client.json(
        "POST", "/api/decisions", {"id": row["id"], "status": "maybe"}
    )
    assert status == 400


def test_choosing_a_candidate_accepts_it_and_leaves_the_others_pending(served):
    client, cfg, _ = served
    wren = people_by_name(client)["Wren Calloway"]
    first, second = (c["change"] for c in wren["candidates"])
    status, _ = client.json("POST", "/api/link", {"person": 3, "change": first})
    assert status == 200
    assert load_decisions(cfg.decisions_path) == {first: Decision(ACCEPTED)}
    assert second not in load_decisions(cfg.decisions_path)
    chosen = people_by_name(client)["Wren Calloway"]
    assert chosen["state"] == "unchanged"
    assert chosen["mine"]["id"] == "I-w1"
    preview = client.json("GET", "/api/preview")[1]
    assert [i["action"] for i in preview["items"]] == ["Link to existing person"]
    client.json("POST", "/api/link", {"person": 3, "change": None})
    assert load_decisions(cfg.decisions_path) == {}
    assert people_by_name(client)["Wren Calloway"]["state"] == "needs_match"


def test_link_refuses_a_candidate_of_another_person(served):
    client, _, _ = served
    status, _ = client.json("POST", "/api/link", {"person": 1, "change": "x"})
    assert status == 404
    status, _ = client.json("POST", "/api/link", {"person": 3, "change": "x"})
    assert status == 404


def test_preview_renders_accepted_changes_as_a_plan(served):
    client, cfg, _ = served
    assert client.json("GET", "/api/preview")[1] == {
        "count": 0,
        "items": [],
        "skipped": [],
        "command": apply_command(Path("pages"), cfg.decisions_path.parent),
    }
    birth, note = first_row(client), first_row(client, "Note")
    new_person = people_by_name(client)["Ivy Marlowe"]["changes"][0]
    client.json("POST", "/api/decisions", {"id": birth["id"], "status": "accepted"})
    client.json("POST", "/api/decisions", {"id": note["id"], "status": "accepted"})
    client.json(
        "POST", "/api/decisions", {"id": new_person["id"], "status": "accepted"}
    )
    plan = client.json("GET", "/api/preview")[1]
    assert plan["count"] == 3
    actions = {(i["action"], i["label"]): i for i in plan["items"]}
    assert actions[("Change", "Birth")]["lines"] == ["1850, Lyon"]
    assert actions[("Add", "Note")]["sensitive"] is True
    assert actions[("Create person", "New person")]["person"] == "Ivy Marlowe"


def test_static_files_and_security_headers(served):
    client, _, _ = served
    response, raw = client.request("GET", "/", token=False)
    assert response.status == 200
    assert b"__REVIEW_TOKEN__" not in raw
    assert client.token.encode() in raw
    assert "script-src 'self'" in response.getheader("Content-Security-Policy")
    for name in ("app.js", "app.css", "tokens.css", "ui.css"):
        response, _ = client.request("GET", f"/{name}", token=False)
        assert response.status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/../pyproject.toml",
        "/%2e%2e/%2e%2e/pyproject.toml",
        "/..%2fpyproject.toml",
        "/web/../app.js/../../server.py",
        "/server.py",
        "/index.html.bak",
        "/media/1/../secret.txt",
        "/media/1/%2e%2e%2fsecret.txt",
        "/media/..%2fmedia/secret.txt",
        "/media/1/secret.txt",
        "/media/1/missing.jpg",
        "/media/x/7-Harvest.jpg",
        "/media/1/7-Harvest.jpg%00.txt",
    ],
)
def test_paths_outside_the_served_set_are_refused(served, path):
    client, _, _ = served
    response, raw = client.request("GET", path, token=False)
    assert response.status == 404
    assert b"private" not in raw


def test_local_photo_is_served(served):
    client, _, _ = served
    response, raw = client.request("GET", "/media/1/7-Harvest.jpg", token=False)
    assert response.status == 200
    assert response.getheader("Content-Type") == "image/jpeg"
    assert raw == b"\xff\xd8photo"


def test_non_get_on_static_paths_is_refused(served):
    client, _, _ = served
    response, _ = client.request("POST", "/app.js", {})
    assert response.status == 405


def test_web_assets_make_no_external_requests():
    for name in ("index.html", "app.js", "app.css"):
        text = (WEB_DIR / name).read_text()
        assert "http://" not in text.replace("http://www.w3.org", "")
        assert "https://" not in text


@pytest.mark.parametrize("name", ["tokens.css", "ui.css"])
def test_shared_styles_match_the_extension(name):
    assert (WEB_DIR / name).read_bytes() == (REPO / "extension" / name).read_bytes()


def test_review_command_rejects_a_missing_pages_directory(tmp_path):
    cfg = Config("example", "http://gramps.test", tmp_path, tmp_path)
    with pytest.raises(ConfigError, match="not a directory"):
        review_tree(cfg, tmp_path / "missing", tmp_path / "tree.json", 0, print)


def test_review_command_rejects_an_invalid_port(tmp_path):
    cfg = Config("example", "http://gramps.test", tmp_path, tmp_path)
    with pytest.raises(ConfigError, match="not a port"):
        review_tree(cfg, tmp_path, tmp_path / "tree.json", 70000, print)


def test_link_ids_are_not_decided_through_the_decisions_endpoint(served):
    client, cfg, _ = served
    candidate = people_by_name(client)["Wren Calloway"]["candidates"][0]["change"]
    status, body = client.json(
        "POST", "/api/decisions", {"id": candidate, "status": "rejected"}
    )
    assert status == 400
    assert "link" in body["error"]
    assert not cfg.decisions_path.exists()


def test_corrupt_decisions_file_gives_a_readable_error(served):
    client, cfg, _ = served
    cfg.decisions_path.write_text("{not json")
    row = {"id": "x", "status": "accepted"}
    for method, path, body in (
        ("GET", "/api/changes", None),
        ("GET", "/api/preview", None),
        ("POST", "/api/decisions", row),
        ("POST", "/api/link", {"person": 3, "change": None}),
    ):
        status, payload = client.json(method, path, body)
        assert status == 500
        assert "decisions" in payload["error"]


def test_non_integer_content_length_is_a_bad_request(served):
    client, _, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
    connection.putrequest("POST", "/api/decisions")
    for name, value in (
        (TOKEN_HEADER, client.token),
        ("Content-Type", "application/json"),
        ("Content-Length", "abc"),
    ):
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 400
    connection.close()


def test_review_command_reports_a_port_that_cannot_be_bound(tmp_path, served):
    _, _, server = served
    cfg = Config("example", "http://gramps.test", tmp_path, tmp_path)
    with pytest.raises(ConfigError, match="listen"):
        serve(cfg, synthetic_review(None), server.server_address[1], print)


def test_a_choice_offered_through_a_relative_can_be_undone(tmp_path):
    app = App(relative_review(), tmp_path / "decisions.json")
    offered = people_view(app.review, {}, derive(app.review, {}))[1]["candidates"][0][
        "change"
    ]
    app.link({"person": 2, "change": offered})
    assert app.decisions() == {offered: Decision(ACCEPTED)}
    app.link({"person": 2, "change": None})
    assert app.decisions() == {}


def test_a_change_that_waits_on_another_person_cannot_be_decided(tmp_path):
    app = App(waiting_review(), tmp_path / "decisions.json")
    waiting = next(c for c in derive(app.review, {}).changes if c.waiting_on)
    with pytest.raises(RequestError) as error:
        app.decide({"id": waiting.id, "status": "accepted"})
    assert error.value.status == 400
    assert not app.decisions_path.exists()


def test_places_lists_his_texts_and_saves_and_clears_a_mapping(served):
    client, cfg, _ = served
    status, payload = client.json("GET", "/api/places")
    assert status == 200
    (lyon,) = payload["places"]
    assert (lyon["text"], lyon["uses"], lyon["mapping"]) == ("Lyon", 1, None)
    assert [s["handle"] for s in lyon["suggestions"]] == ["pl1"]
    saved = {"text": "Lyon", "handle": "pl1"}
    assert client.json("POST", "/api/places", saved)[0] == 200
    assert load_decisions(cfg.decisions_path) == {"place:Lyon": Decision(EDITED, "pl1")}
    assert client.json("GET", "/api/places")[1]["places"][0]["mapping"] == {
        "handle": "pl1",
        "title": "Lyon, Rhone, France",
    }
    assert (
        client.json("POST", "/api/places", {"text": "Lyon", "handle": None})[0] == 200
    )
    assert load_decisions(cfg.decisions_path) == {}


def test_places_rejects_an_unknown_text_or_handle(served):
    client, cfg, _ = served
    for body in (
        {"text": "Nowhere", "handle": "pl1"},
        {"text": "Lyon", "handle": "missing"},
        {"text": 5, "handle": "pl1"},
        {"text": ["Lyon"], "handle": "pl1"},
        {"text": "Lyon", "handle": ["pl1"]},
    ):
        assert client.json("POST", "/api/places", body)[0] == 400
    assert not cfg.decisions_path.exists()


def test_a_place_decision_is_not_a_change(served):
    client, _, _ = served
    client.json("POST", "/api/places", {"text": "Lyon", "handle": "pl1"})
    status, payload = client.json("GET", "/api/changes")
    assert status == 200
    assert payload["summary"]["accepted"] == 0
    assert client.json("GET", "/api/preview")[1]["count"] == 0


def test_changes_carry_every_gramps_place_title_for_the_datalist(served):
    client, _, _ = served
    payload = client.json("GET", "/api/changes")[1]
    assert payload["place_titles"] == [
        {"handle": "pl1", "title": "Lyon, Rhone, France"}
    ]


def test_a_row_shows_the_place_chosen_for_his_text(served):
    client, _, _ = served

    def birth_place():
        row = first_row(client, "Birth")
        return row["his_place"], row["id"]

    before, change_id = birth_place()
    assert before is None
    client.json("POST", "/api/places", {"text": "Lyon", "handle": "pl1"})
    after, same_id = birth_place()
    assert (after, same_id) == ("Lyon, Rhone, France", change_id)


def test_none_of_these_refuses_the_person_and_can_be_undone(served):
    client, cfg, _ = served
    status, _ = client.json("POST", "/api/link", {"person": 3, "change": "none"})
    assert status == 200
    assert load_decisions(cfg.decisions_path) == {
        f"{NONE_PREFIX}3": Decision("rejected")
    }
    wren = people_by_name(client)["Wren Calloway"]
    assert (wren["state"], wren["refused"], wren["candidates"]) == ("new", True, [])
    status, _ = client.json("POST", "/api/link", {"person": 3, "change": None})
    assert status == 200
    assert load_decisions(cfg.decisions_path) == {}
    wren = people_by_name(client)["Wren Calloway"]
    assert (wren["state"], wren["refused"], len(wren["candidates"])) == (
        "needs_match",
        False,
        2,
    )


def test_none_of_these_is_refused_for_a_person_with_no_candidates(served):
    client, cfg, _ = served
    status, _ = client.json("POST", "/api/link", {"person": 1, "change": "none"})
    assert status == 404
    assert not cfg.decisions_path.exists()


def test_link_refuses_a_person_who_is_not_in_his_tree(served):
    client, cfg, _ = served
    save_decisions(cfg.decisions_path, {f"{NONE_PREFIX}999": Decision("rejected")})
    for body in ({"person": 999, "change": None}, {"person": "3", "change": "none"}):
        status, _ = client.json("POST", "/api/link", body)
        assert status == 404
    assert load_decisions(cfg.decisions_path) == {
        f"{NONE_PREFIX}999": Decision("rejected")
    }


def test_choosing_after_refusing_replaces_the_refusal(tmp_path):
    app = App(synthetic_review(None), tmp_path / "decisions.json")
    first = people_view(app.review, {}, derive(app.review, {}))[2]["candidates"][0][
        "change"
    ]
    save_decisions(app.decisions_path, {f"{NONE_PREFIX}3": Decision("rejected")})
    app.link({"person": 3, "change": first})
    assert app.decisions() == {first: Decision(ACCEPTED)}


def test_none_replaces_earlier_choices_of_that_person(tmp_path):
    app = App(synthetic_review(None), tmp_path / "decisions.json")
    first = people_view(app.review, {}, derive(app.review, {}))[2]["candidates"][0][
        "change"
    ]
    app.link({"person": 3, "change": first})
    app.link({"person": 3, "change": "none"})
    assert app.decisions() == {f"{NONE_PREFIX}3": Decision("rejected")}


def test_switching_a_link_replaces_the_earlier_choice(served):
    client, cfg, _ = served
    first, second = (
        c["change"] for c in people_by_name(client)["Wren Calloway"]["candidates"]
    )
    client.json("POST", "/api/link", {"person": 3, "change": first})
    status, _ = client.json("POST", "/api/link", {"person": 3, "change": second})
    assert status == 200
    assert load_decisions(cfg.decisions_path) == {second: Decision(ACCEPTED)}


def test_an_unexpected_failure_is_json_and_reaches_the_terminal(
    served, monkeypatch, capsys
):
    def broken(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(App, "changes", broken)
    client, _, _ = served
    status, payload = client.json("GET", "/api/changes")
    assert status == 500
    assert payload == {"error": "The review server failed; see the terminal."}
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_a_refused_status_is_quoted_in_the_error(served):
    client, _, _ = served
    row = first_row(client)
    status, payload = client.json(
        "POST", "/api/decisions", {"id": row["id"], "status": "maybe"}
    )
    assert status == 400
    assert payload["error"] == 'unknown decision status "maybe"'


@pytest.fixture
def thumbnails(tmp_path):
    folder = tmp_path / "thumbnails"
    folder.mkdir()
    (folder / "m1.jpg").write_bytes(b"\xff\xd8thumb")
    (folder / "m9.jpg").write_bytes(b"\xff\xd8stranger")
    (tmp_path / "secret.jpg").write_bytes(b"private")
    return folder


@pytest.fixture
def served_thumbnails(tmp_path, thumbnails):
    cfg = Config("example", "http://gramps.test", tmp_path / "data", tmp_path)
    cfg.data_dir.mkdir()
    review = synthetic_review(None, thumbnails)
    held = GrampsMedia("m1", "Portrait", "a.jpg", "image/jpeg")
    people = tuple(
        replace(p, media=(held,)) if p.handle == "h1" else p for p in review.tree.people
    )
    review = replace(review, tree=replace(review.tree, people=people))
    server = make_server(cfg, review, 0)
    app = server.RequestHandlerClass.app
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield Client(server, app.token)
    server.shutdown()
    server.server_close()


def test_a_saved_gramps_thumbnail_is_served(served_thumbnails):
    response, raw = served_thumbnails.request("GET", "/thumbnails/m1.jpg", token=False)
    assert (response.status, raw) == (200, b"\xff\xd8thumb")
    assert response.getheader("Content-Type") == "image/jpeg"


@pytest.mark.parametrize(
    "path",
    [
        "/thumbnails/m9.jpg",
        "/thumbnails/m2.jpg",
        "/thumbnails/../secret.jpg",
        "/thumbnails/..%2fsecret.jpg",
        "/thumbnails/m1.png",
        "/thumbnails/m1.jpg/x",
        "/thumbnails/",
    ],
)
def test_thumbnails_refuse_unknown_handles_and_traversal(served_thumbnails, path):
    response, _ = served_thumbnails.request("GET", path, token=False)
    assert response.status == 404


def test_thumbnails_are_not_served_without_a_folder(served):
    client, _, _ = served
    response, _ = client.request("GET", "/thumbnails/m1.jpg", token=False)
    assert response.status == 404
