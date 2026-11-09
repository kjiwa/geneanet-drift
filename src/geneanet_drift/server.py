import hmac
import json
import mimetypes
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from geneanet_drift.config import Config, ConfigError
from geneanet_drift.decisions import (
    ACCEPTED,
    EDITED,
    Decision,
    DecisionError,
    load_decisions,
    save_decisions,
)
from geneanet_drift.review_data import (
    PHOTO_TYPES,
    Review,
    derive,
    editable_kind,
    field_changes,
    is_link,
    link_changes,
    people_view,
    preview_view,
    summary_view,
    validate_edit,
)

HOST = "127.0.0.1"
WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = ("app.js", "app.css", "tokens.css", "ui.css")
TOKEN_HEADER = "X-Review-Token"
TOKEN_PLACEHOLDER = "__REVIEW_TOKEN__"
PENDING = "pending"
MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
    "style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class App:
    review: Review
    decisions_path: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def decisions(self) -> dict[str, Decision]:
        return load_decisions(self.decisions_path)

    def changes(self) -> dict:
        decisions = self.decisions()
        people = people_view(self.review, decisions)
        accepted = preview_view(self.review, decisions)["count"]
        return {"summary": summary_view(people, accepted), "people": people}

    def preview(self) -> dict:
        return preview_view(self.review, self.decisions())

    def decide(self, body: dict) -> dict:
        with self.lock:
            decisions = self.decisions()
            change_id = body.get("id")
            changes = derive(self.review, decisions).changes
            if not isinstance(change_id, str) or change_id not in {
                c.id for c in changes
            }:
                raise RequestError(HTTPStatus.NOT_FOUND, "unknown change")
            if change_id in {c.id for c in changes if is_link(c)}:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST, "choose a match through the link action"
                )
            save_decisions(
                self.decisions_path,
                self._apply(
                    decisions, change_id, body.get("status"), body.get("value")
                ),
            )
        return {"ok": True}

    def _apply(self, decisions, change_id, status, value) -> dict[str, Decision]:
        kept = {k: d for k, d in decisions.items() if k != change_id}
        if status == PENDING:
            return kept
        try:
            if status == EDITED:
                value = validate_edit(self._editable(decisions, change_id), value)
            return {**kept, change_id: Decision(status, value)}
        except (DecisionError, ValueError) as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, str(error)) from error

    def _editable(self, decisions, change_id: str) -> str | None:
        change = next(
            (c for c in field_changes(self.review, decisions) if c.id == change_id),
            None,
        )
        return editable_kind(change) if change else None

    def link(self, body: dict) -> dict:
        person, chosen = body.get("person"), body.get("change")
        with self.lock:
            decisions = self.decisions()
            siblings = [
                c.id for c in link_changes(self.review, decisions) if c.person == person
            ]
            if not siblings or (chosen is not None and chosen not in siblings):
                raise RequestError(HTTPStatus.NOT_FOUND, "unknown match")
            decisions = {k: d for k, d in decisions.items() if k not in siblings}
            if chosen is not None:
                decisions[chosen] = Decision(ACCEPTED)
            save_decisions(self.decisions_path, decisions)
        return {"ok": True}

    def media_path(self, key: str, name: str) -> Path | None:
        root = self.review.media_dir
        if root is None or not key.isdigit() or Path(name).name != name:
            return None
        try:
            path = (root / f"person-{key}" / name).resolve()
        except (OSError, ValueError):
            return None
        inside = path.is_relative_to(root.resolve())
        if inside and path.is_file() and path.suffix.lower() in PHOTO_TYPES:
            return path
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "geneanet-drift"
    app: App

    def log_message(self, format, *args):
        pass

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"{HOST}:{port}", f"localhost:{port}")

    def _token_ok(self) -> bool:
        sent = self.headers.get(TOKEN_HEADER, "")
        return hmac.compare_digest(sent.encode(), self.app.token.encode())

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from error
        if not 0 < length <= MAX_BODY:
            raise RequestError(HTTPStatus.BAD_REQUEST, "bad body size")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid JSON") from error
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "expected an object")
        return body

    def _api(self, path: str, method: str) -> dict:
        if not self._token_ok():
            raise RequestError(HTTPStatus.FORBIDDEN, "missing or wrong token")
        app = self.app
        if method == "GET" and path == "/api/changes":
            return app.changes()
        if method == "GET" and path == "/api/preview":
            return app.preview()
        if method == "POST" and path == "/api/decisions":
            return app.decide(self._body())
        if method == "POST" and path == "/api/link":
            return app.link(self._body())
        raise RequestError(HTTPStatus.NOT_FOUND, "not found")

    def _static(self, path: str) -> None:
        name = "index.html" if path == "/" else path.removeprefix("/")
        if name not in (*STATIC_FILES, "index.html"):
            raise RequestError(HTTPStatus.NOT_FOUND, "not found")
        data = (WEB_DIR / name).read_bytes()
        if name == "index.html":
            data = data.replace(TOKEN_PLACEHOLDER.encode(), self.app.token.encode())
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, data, f"{kind}; charset=utf-8")

    def _media(self, path: str) -> None:
        parts = path.split("/")
        found = self.app.media_path(parts[2], parts[3]) if len(parts) == 4 else None
        if found is None:
            raise RequestError(HTTPStatus.NOT_FOUND, "not found")
        kind = mimetypes.guess_type(found.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, found.read_bytes(), kind)

    def _route(self, method: str) -> None:
        try:
            if not self._host_ok():
                raise RequestError(HTTPStatus.FORBIDDEN, "unexpected Host")
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/api/"):
                self._json(HTTPStatus.OK, self._api(path, method))
            elif method != "GET":
                raise RequestError(HTTPStatus.METHOD_NOT_ALLOWED, "read only")
            elif path.startswith("/media/"):
                self._media(path)
            else:
                self._static(path)
        except RequestError as error:
            self._json(error.status, {"error": str(error)})
        except DecisionError as error:
            message = f"The saved decisions cannot be read: {error}"
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": message})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def make_server(cfg: Config, review: Review, port: int = 0) -> ThreadingHTTPServer:
    app = App(review, cfg.decisions_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((HOST, port), handler)


def serve(cfg: Config, review: Review, port: int, say: Callable[[str], None]) -> int:
    try:
        server = make_server(cfg, review, port)
    except OSError as error:
        raise ConfigError(f"cannot listen on {HOST}:{port}: {error}") from error
    say(f"Review at http://{HOST}:{server.server_address[1]}/ (Ctrl+C to stop).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("")
    finally:
        server.server_close()
    return 0
