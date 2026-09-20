from datetime import date, datetime
from pathlib import Path

import pytest

from geneanet_drift.config import (
    ConfigError,
    Run,
    State,
    end_of_day,
    ensure_outside_repo,
    find_repo_root,
    load_config,
    load_run,
    load_state,
    save_run,
    save_state,
)

CONFIG = 'tree = "example"\ngramps_url = "https://gramps.test/"\n'


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_find_repo_root_walks_up(tmp_path):
    repo = make_repo(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == repo
    assert find_repo_root(tmp_path / "elsewhere") is None


def test_data_dir_inside_own_repo_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(CONFIG)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    with pytest.raises(ConfigError, match="own git work tree"):
        load_config(str(data_dir), env, repo_root=repo)


def test_cache_dir_inside_own_repo_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(CONFIG)
    with pytest.raises(ConfigError, match="own git work tree"):
        load_config(
            str(data_dir), {"XDG_CACHE_HOME": str(repo / "cache")}, repo_root=repo
        )


def test_directory_outside_repo_is_accepted(tmp_path):
    ensure_outside_repo(tmp_path / "data", make_repo(tmp_path))


def test_data_dir_comes_from_environment_and_gramps_url_is_overridable(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(CONFIG)
    env = {
        "GENEANET_DRIFT_DATA": str(data_dir),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "GRAMPS_URL": "https://override.test",
    }
    cfg = load_config(None, env, repo_root=tmp_path / "no-repo")
    assert cfg.data_dir == data_dir
    assert cfg.gramps_url == "https://override.test"
    assert cfg.cache_dir.is_dir()


def test_missing_config_names_the_file(tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(str(tmp_path / "empty"), env, repo_root=tmp_path / "no-repo")


def test_state_round_trips_and_defaults_empty(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == State()
    moment = datetime(2026, 8, 25, 11, 25, 15)
    save_state(path, State(moment))
    assert load_state(path) == State(moment)


def test_a_date_only_last_sync_means_the_end_of_that_day(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"last_sync": "2026-04-02", "individuals": 399}')
    assert load_state(path) == State(end_of_day(date(2026, 4, 2)))
    assert load_state(path).last_sync > datetime(2026, 4, 2, 23, 59, 58)


def test_run_round_trips_and_defaults_empty(tmp_path):
    path = tmp_path / "run.json"
    assert load_run(path) == Run()
    run = Run(date(2023, 7, 15), {"tariq zorvane": "I0001"}, datetime(2026, 8, 25, 16))
    save_run(path, run)
    assert load_run(path) == run
