import json
from dataclasses import replace
from pathlib import Path

import pytest

from geneanet_drift import wizard
from geneanet_drift.config import ConfigError
from geneanet_drift.gramps import GrampsError, take_tree
from geneanet_drift.tree_file import read_tree, tree_from_json, tree_to_json, write_tree
from geneanet_drift.wizard import main, snapshot_tree


def test_tree_round_trips_through_a_file(tree_client, tmp_path):
    tree = take_tree(tree_client)
    path = tmp_path / "tree.json"
    write_tree(path, tree)
    assert read_tree(path) == tree
    assert tree.people[0].events and tree.families[1].events


def test_serialised_tree_is_plain_json(tree_client):
    text = json.dumps(tree_to_json(take_tree(tree_client)))
    assert json.loads(text)["format"] == 1


@pytest.mark.parametrize("raw", [{}, {"format": 2}])
def test_unknown_format_is_refused(raw):
    with pytest.raises(GrampsError, match="format"):
        tree_from_json(raw)


def test_non_object_snapshot_is_refused():
    with pytest.raises(GrampsError):
        tree_from_json([])


def test_truncated_snapshot_is_refused(tree_client):
    raw = tree_to_json(take_tree(tree_client))
    del raw["people"][0]["events"]
    with pytest.raises(GrampsError, match="unreadable"):
        tree_from_json(raw)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(GrampsError, match="does not exist"):
        read_tree(tmp_path / "absent.json")


def test_snapshot_writes_the_tree_with_the_read_client(
    cfg, tree_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(wizard, "_connect", lambda cfg, ask: tree_client)
    said: list[str] = []
    out = tmp_path / "tree.json"
    assert snapshot_tree(cfg, out, input, said.append) == 0
    assert len(read_tree(out).people) == 5
    assert said[-1].startswith("Wrote 5 people, 3 families")


def test_snapshot_refuses_a_path_inside_the_repository(cfg, tree_client, monkeypatch):
    monkeypatch.setattr(wizard, "_connect", lambda cfg, ask: tree_client)
    out = Path(__file__).parent / "tree.json"
    with pytest.raises(ConfigError, match="--out"):
        snapshot_tree(cfg, out, input, print)
    assert not out.exists()


def test_snapshot_command_requires_an_output_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--data-dir", str(tmp_path), "snapshot"])
    assert exit_.value.code == 2
    assert "--out" in capsys.readouterr().err


def test_snapshot_command_reaches_the_snapshot_step(
    cfg, tree_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(wizard, "_connect", lambda cfg, ask: tree_client)
    monkeypatch.setattr(wizard, "load_config", lambda data_dir, env: replace(cfg))
    out = tmp_path / "tree.json"
    assert main(["snapshot", "--out", str(out)]) == 0
    assert out.is_file()


@pytest.mark.parametrize("text", ["{not json", "[]", '{"format": 1, "people": 3}'])
def test_malformed_snapshot_file_raises_gramps_error(tmp_path, text):
    path = tmp_path / "tree.json"
    path.write_text(text)
    with pytest.raises(GrampsError):
        read_tree(path)


def test_snapshot_checks_the_output_directory_before_connecting(cfg, monkeypatch):
    def connect(cfg, ask):
        raise AssertionError("fetched before checking --out")

    monkeypatch.setattr(wizard, "_connect", connect)
    with pytest.raises(ConfigError, match="--out"):
        snapshot_tree(cfg, cfg.data_dir / "absent" / "tree.json", input, print)
    with pytest.raises(ConfigError, match="--out"):
        snapshot_tree(cfg, Path(__file__).parent / "tree.json", input, print)
