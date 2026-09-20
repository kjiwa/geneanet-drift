import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from geneanet_drift.config import Config, load_config
from geneanet_drift.gramps import GrampsClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://gramps.test"


def make_token(*permissions: str) -> str:
    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return (
        f"{segment({'alg': 'none'})}.{segment({'permissions': list(permissions)})}.sig"
    )


TOKEN = make_token("ViewPrivate")


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeGramps:
    """Replays the synthetic recorded responses, paging list routes like the API."""

    def __init__(self, with_links: bool = True, token: str = TOKEN):
        self.token = token
        self.requests: list[tuple[str, str]] = []
        self.routes = {
            "/api/metadata/": load_fixture("metadata.json"),
            "/api/people/": load_fixture("people.json"),
            "/api/events/": load_fixture("events.json"),
            "/api/sources/": load_fixture("sources.json"),
            "/api/citations/": load_fixture("citations.json") if with_links else [],
            "/api/citations/c1": load_fixture("citation_c1.json"),
        }

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        self.requests.append((method, parts.path))
        if method == "POST":
            return 200, {}, json.dumps({"access_token": self.token}).encode()
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {}, b"{}"
        payload = self.routes[parts.path]
        response_headers: dict[str, str] = {}
        if isinstance(payload, list):
            query = parse_qs(parts.query)
            size = int(query["pagesize"][0])
            start = (int(query["page"][0]) - 1) * size
            response_headers["x-total-count"] = str(len(payload))
            payload = payload[start : start + size]
        return 200, response_headers, json.dumps(payload).encode()


@pytest.fixture
def feed_text() -> str:
    return (FIXTURES / "feed.txt").read_text()


@pytest.fixture
def fake_gramps() -> FakeGramps:
    return FakeGramps()


def make_client(transport) -> GrampsClient:
    return GrampsClient(BASE_URL, "reader", "secret", transport=transport)


@pytest.fixture
def client(fake_gramps: FakeGramps) -> GrampsClient:
    return make_client(fake_gramps)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(
        f'tree = "example"\ngramps_url = "{BASE_URL}"\npriority_surnames = ["Quillon"]\n'
    )
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    return load_config(str(data_dir), env, repo_root=tmp_path / "no-repo")
