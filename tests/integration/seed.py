"""Creates the three integration users and imports seed.gramps into a running compose stack.

    docker compose -f tests/integration/compose.yaml up -d
    python tests/integration/seed.py

Re-running on a seeded stack fails at the import step; `docker compose down -v` resets it.
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BASE_URL = "http://127.0.0.1:18080"
# (user, role): Owner 4 imports files, Editor 3 writes, Member 1 reads private records.
USERS = (("it-admin", 4), ("it-editor", 3), ("it-reader", 1))
# The token endpoint allows one request per second.
TOKEN_INTERVAL = 1.2


def add_users() -> None:
    for user, role in USERS:
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(HERE / "compose.yaml"),
                "exec",
                "-T",
                "grampsweb",
                "python3",
                "-m",
                "gramps_webapi",
                "user",
                "add",
                "--role",
                str(role),
                user,
                f"{user}-pw",
            ],
            check=True,
        )


def call(
    path: str, token: str | None, data: bytes | None = None, ctype="application/json"
):
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE_URL + path, data, headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def admin_token() -> str:
    time.sleep(TOKEN_INTERVAL)
    credentials = json.dumps({"username": "it-admin", "password": "it-admin-pw"})
    return call("/api/token/", None, credentials.encode())["access_token"]


def import_seed(token: str) -> dict:
    data = (HERE / "seed.gramps").read_bytes()
    task = call("/api/importers/gramps/file", token, data, "application/octet-stream")
    task_id = task["task"]["id"]
    while True:
        status = call(f"/api/tasks/{task_id}", token)
        if status["state"] in ("SUCCESS", "FAILURE"):
            return status
        time.sleep(1)


def main() -> int:
    add_users()
    status = import_seed(admin_token())
    print(status["state"], status.get("result_object"))
    return 0 if status["state"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
