import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    tree: str
    gramps_url: str
    priority_surnames: tuple[str, ...]
    data_dir: Path
    gramps_user: str | None = None
    gramps_password: str | None = field(default=None, repr=False)
    gramps_write_user: str | None = None
    gramps_write_password: str | None = field(default=None, repr=False)
    warnings: tuple[str, ...] = ()

    @property
    def negatives_path(self) -> Path:
        return self.data_dir / "negatives.json"

    @property
    def decisions_path(self) -> Path:
        return self.data_dir / "decisions.json"

    @property
    def applied_path(self) -> Path:
        return self.data_dir / "applied.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


def find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ensure_outside_repo(path: Path, repo_root: Path | None) -> None:
    if repo_root is not None and path.resolve().is_relative_to(repo_root.resolve()):
        raise ConfigError(
            f"{path} is inside this tool's own git work tree ({repo_root}); "
            "point --data-dir / GENEANET_DRIFT_DATA at a directory outside it"
        )


def _xdg(env: Mapping[str, str], variable: str, fallback: str) -> Path:
    return Path(env.get(variable) or Path.home() / fallback)


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        if not separator:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        values[key.strip()] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _env_file_warnings(path: Path) -> tuple[str, ...]:
    if path.is_file() and path.stat().st_mode & 0o077:
        return (f"{path} is readable by other users; run chmod 600 on it",)
    return ()


def load_config(
    data_dir_arg: str | None,
    env: Mapping[str, str],
    repo_root: Path | None = None,
) -> Config:
    if repo_root is None:
        repo_root = find_repo_root(Path(__file__).resolve().parent)
    data_dir = Path(
        data_dir_arg
        or env.get("GENEANET_DRIFT_DATA")
        or _xdg(env, "XDG_DATA_HOME", ".local/share") / "geneanet-drift"
    )
    ensure_outside_repo(data_dir, repo_root)

    env_path = data_dir / ".env"
    env = {**read_env_file(env_path), **env}

    config_path = data_dir / "config.toml"
    if not config_path.is_file():
        raise ConfigError(f"missing {config_path}; copy config.example.toml there")
    raw = tomllib.loads(config_path.read_text())
    gramps_url = env.get("GRAMPS_URL") or raw.get("gramps_url")
    if not raw.get("tree") or not gramps_url:
        raise ConfigError(
            f"{config_path} needs `tree` and `gramps_url` (or GRAMPS_URL)"
        )

    return Config(
        tree=raw["tree"],
        gramps_url=gramps_url.rstrip("/"),
        priority_surnames=tuple(raw.get("priority_surnames", [])),
        data_dir=data_dir,
        gramps_user=env.get("GRAMPS_USER") or raw.get("gramps_user"),
        gramps_password=env.get("GRAMPS_PASSWORD"),
        gramps_write_user=env.get("GRAMPS_WRITE_USER") or raw.get("gramps_write_user"),
        gramps_write_password=env.get("GRAMPS_WRITE_PASSWORD"),
        warnings=_env_file_warnings(env_path),
    )


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def write_json_atomic(path: Path, value) -> None:
    """Writes a private (0600) file that is either the old or the new content."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def end_of_day(day: date) -> datetime:
    return datetime.combine(day, time.max)
