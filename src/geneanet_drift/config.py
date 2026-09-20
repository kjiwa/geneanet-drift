import json
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
    cache_dir: Path

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def negatives_path(self) -> Path:
        return self.data_dir / "negatives.json"

    @property
    def pending_path(self) -> Path:
        return self.data_dir / "pending.json"

    @property
    def feed_path(self) -> Path:
        return self.cache_dir / "feed.txt"

    @property
    def review_path(self) -> Path:
        return self.cache_dir / "review.md"

    @property
    def run_path(self) -> Path:
        return self.cache_dir / "run.json"


@dataclass(frozen=True)
class State:
    last_sync: datetime | None = None


@dataclass(frozen=True)
class Run:
    """Answers kept while a run is open, so an interrupted wizard resumes."""

    window_start: date | None = None
    accepted: dict[str, str] = field(default_factory=dict)  # name key -> Gramps ID
    next_sync: datetime | None = None  # set once the review is written


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
    cache_dir = _xdg(env, "XDG_CACHE_HOME", ".cache") / "geneanet-drift"
    for directory in (data_dir, cache_dir):
        ensure_outside_repo(directory, repo_root)

    config_path = data_dir / "config.toml"
    if not config_path.is_file():
        raise ConfigError(f"missing {config_path}; copy config.example.toml there")
    raw = tomllib.loads(config_path.read_text())
    gramps_url = env.get("GRAMPS_URL") or raw.get("gramps_url")
    if not raw.get("tree") or not gramps_url:
        raise ConfigError(
            f"{config_path} needs `tree` and `gramps_url` (or GRAMPS_URL)"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        tree=raw["tree"],
        gramps_url=gramps_url.rstrip("/"),
        priority_surnames=tuple(raw.get("priority_surnames", [])),
        data_dir=data_dir,
        cache_dir=cache_dir,
    )


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def end_of_day(day: date) -> datetime:
    return datetime.combine(day, time.max)


def _parse_moment(raw: str) -> datetime:
    try:
        return end_of_day(date.fromisoformat(raw))
    except ValueError:
        return datetime.fromisoformat(raw)


def _read_moment(raw: str | None) -> datetime | None:
    return _parse_moment(raw) if raw else None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def load_state(path: Path) -> State:
    return State(_read_moment(read_json(path, {}).get("last_sync")))


def save_state(path: Path, state: State) -> None:
    write_json(path, {"last_sync": _iso(state.last_sync)})


def load_run(path: Path) -> Run:
    raw = read_json(path, {})
    window_start = raw.get("window_start")
    return Run(
        window_start=date.fromisoformat(window_start) if window_start else None,
        accepted=dict(raw.get("accepted", {})),
        next_sync=_read_moment(raw.get("next_sync")),
    )


def save_run(path: Path, run: Run) -> None:
    write_json(
        path,
        {
            "window_start": run.window_start.isoformat() if run.window_start else None,
            "accepted": run.accepted,
            "next_sync": _iso(run.next_sync),
        },
    )
