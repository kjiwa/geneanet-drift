import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from geneanet_drift.feed import name_key

PAGE_SIZE = 500
REQUEST_TIMEOUT = 30

Transport = Callable[
    [str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]
]


PRIVATE_PERMISSION = "ViewPrivate"
OWN_USER = "EditOwnUser"
# Prefixes of the permissions observed on an admin token that mutate data.
WRITE_PREFIXES = (
    "Add",
    "Edit",
    "Delete",
    "BatchDelete",
    "Import",
    "Make",
    "Repair",
    "Trigger",
    "Upgrade",
    "Disable",
)


class GrampsError(Exception):
    pass


def _token_permissions(token: str) -> frozenset[str]:
    """Reads the unverified permissions claim; the server enforces it regardless."""
    try:
        segment = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
        return frozenset(claims["permissions"])
    except (IndexError, ValueError, KeyError, TypeError) as error:
        raise GrampsError(
            "the access token has no readable permissions claim"
        ) from error


@dataclass(frozen=True)
class GrampsPerson:
    handle: str
    gramps_id: str
    given: str
    surname: str
    birth_year: int | None = None
    death_year: int | None = None

    @property
    def name(self) -> str:
        return f"{self.given} {self.surname}".strip()


@dataclass(frozen=True)
class Snapshot:
    people: tuple[GrampsPerson, ...]
    families: int
    links: dict[
        str, str
    ]  # name key of his display name -> Gramps ID, read from citations


def urllib_transport(method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            lowered = {key.lower(): value for key, value in response.headers.items()}
            return response.status, lowered, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {}, error.read()


class GrampsClient:
    """Issues GET requests only; the single POST mints the access token."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        transport: Transport = urllib_transport,
    ):
        self._base_url = base_url.rstrip("/")
        self._credentials = {"username": user, "password": password}
        self._transport = transport
        self._token: str | None = None
        self.permissions: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, base_url: str, env: Mapping[str, str]) -> "GrampsClient":
        missing = [v for v in ("GRAMPS_USER", "GRAMPS_PASSWORD") if not env.get(v)]
        if missing:
            raise GrampsError(f"set {' and '.join(missing)} in the environment")
        return cls(base_url, env["GRAMPS_USER"], env["GRAMPS_PASSWORD"])

    def _send(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None = None
    ):
        status, response_headers, payload = self._transport(
            method, self._base_url + path, headers, body
        )
        if status != 200:
            raise GrampsError(f"{method} {path.split('?')[0]} returned HTTP {status}")
        return response_headers, json.loads(payload)

    def _mint_token(self) -> str:
        _, payload = self._send(
            "POST",
            "/api/token/",
            {"Content-Type": "application/json"},
            json.dumps(self._credentials).encode(),
        )
        token = payload["access_token"]
        self.permissions = _token_permissions(token)
        if PRIVATE_PERMISSION not in self.permissions:
            raise GrampsError(
                f"user {self._credentials['username']!r} cannot view private records, "
                "so the snapshot would silently omit them; use a role that has "
                f"{PRIVATE_PERMISSION} but no write permissions"
            )
        return token

    def role_warnings(self) -> list[str]:
        writes = sorted(
            p
            for p in self.permissions
            if p.startswith(WRITE_PREFIXES) and p != OWN_USER
        )
        if not writes:
            return []
        return [
            (
                "this Gramps user has write permissions "
                f"({', '.join(writes)}); the tool only reads, but a read-only role "
                "makes that true by construction"
            )
        ]

    def get(self, path: str, **query) -> tuple[object, int | None]:
        if self._token is None:
            self._token = self._mint_token()
        suffix = f"?{urlencode(query)}" if query else ""
        headers, payload = self._send(
            "GET", path + suffix, {"Authorization": f"Bearer {self._token}"}
        )
        total = headers.get("x-total-count")
        return payload, int(total) if total is not None else None

    def get_all(self, path: str, **query) -> list:
        items: list = []
        page = 1
        while True:
            batch, total = self.get(path, page=page, pagesize=PAGE_SIZE, **query)
            items += batch
            if total is not None and len(items) >= total:
                break
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        if total is not None and len(items) != total:
            raise GrampsError(
                f"{path}: fetched {len(items)} items, X-Total-Count says {total}"
            )
        return items


PERSON_KEYS = (
    "gramps_id,handle,primary_name,birth_ref_index,death_ref_index,event_ref_list"
)


def _event_year(event: dict) -> int | None:
    date = event.get("date") or {}
    dateval = date.get("dateval") or []
    return (dateval[2] if len(dateval) > 2 else 0) or date.get("year") or None


def _ref_year(raw: dict, index_key: str, years: dict[str, int | None]) -> int | None:
    index = raw.get(index_key, -1)
    refs = raw.get("event_ref_list", [])
    return years.get(refs[index]["ref"]) if 0 <= index < len(refs) else None


def _person(raw: dict, years: dict[str, int | None]) -> GrampsPerson:
    name = raw["primary_name"]
    surnames = " ".join(
        s["surname"] for s in name.get("surname_list", []) if s.get("surname")
    )
    return GrampsPerson(
        raw["handle"],
        raw["gramps_id"],
        name.get("first_name", ""),
        surnames,
        _ref_year(raw, "birth_ref_index", years),
        _ref_year(raw, "death_ref_index", years),
    )


def _event_years(client: GrampsClient) -> dict[str, int | None]:
    return {
        event["handle"]: _event_year(event)
        for event in client.get_all("/api/events/", keys="handle,date")
    }


def _geneanet_links(
    client: GrampsClient, source_title: str, ids_by_handle: dict[str, str]
) -> dict[str, str]:
    sources = {
        source["handle"]
        for source in client.get_all("/api/sources/", keys="handle,title")
        if source.get("title") == source_title
    }
    links: dict[str, str] = {}
    citations = client.get_all("/api/citations/", keys="handle,source_handle,page")
    for citation in citations:
        if citation.get("source_handle") not in sources or not citation.get("page"):
            continue
        detail, _ = client.get(f"/api/citations/{citation['handle']}", backlinks=1)
        for handle in detail.get("backlinks", {}).get("person", []):
            if handle in ids_by_handle:
                links[name_key(citation["page"])] = ids_by_handle[handle]
    return links


def take_snapshot(client: GrampsClient, source_title: str) -> Snapshot:
    metadata, _ = client.get("/api/metadata/")
    counts = metadata["object_counts"]
    years = _event_years(client)
    people = tuple(
        _person(raw, years) for raw in client.get_all("/api/people/", keys=PERSON_KEYS)
    )
    if len(people) != counts["people"]:
        raise GrampsError(
            f"snapshot has {len(people)} people, metadata reports {counts['people']}"
        )
    ids_by_handle = {p.handle: p.gramps_id for p in people}
    return Snapshot(
        people,
        counts["families"],
        _geneanet_links(client, source_title, ids_by_handle),
    )
