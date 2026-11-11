from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from geneanet_drift.apply import read_log
from geneanet_drift.config import (
    ConfigError,
    write_json,
)
from geneanet_drift.decisions import ACCEPTED, Decision, save_decisions
from geneanet_drift.diff import diff_people
from geneanet_drift.gramps import References
from geneanet_drift.match import load_negatives
from geneanet_drift.match_tree import match_people
from geneanet_drift.wizard import (
    _connect,
    _connect_writer,
    apply_decisions,
    main,
    undo_applied,
)
from tests.builders import SOURCE, crosswalk, gramps, his, his_event, his_people, tree


def secret_script(answer: str = "typed"):
    prompts: list[str] = []

    def ask_secret(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    return ask_secret, prompts


def with_password(cfg, password: str | None):
    return replace(cfg, gramps_password=password)


@pytest.fixture
def on_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)


def test_connect_prompts_for_an_absent_password(cfg, on_terminal):
    ask_secret, prompts = secret_script()
    _connect(with_password(cfg, None), ask_secret)
    assert len(prompts) == 1
    assert "password" in prompts[0]
    assert "reader" in prompts[0]


def test_connect_does_not_prompt_when_the_password_is_present(cfg, on_terminal):
    ask_secret, prompts = secret_script()
    _connect(with_password(cfg, "given"), ask_secret)
    assert prompts == []


def test_connect_rejects_an_empty_password(cfg, on_terminal):
    ask_secret, _ = secret_script("")
    with pytest.raises(ConfigError, match="empty"):
        _connect(with_password(cfg, None), ask_secret)


def test_connect_without_a_user_names_both_places(cfg):
    ask_secret, _ = secret_script()
    with pytest.raises(ConfigError, match="gramps_user.*GRAMPS_USER"):
        _connect(replace(cfg, gramps_user=None), ask_secret)


def test_connect_without_a_terminal_raises_instead_of_prompting(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    ask_secret, prompts = secret_script()
    with pytest.raises(ConfigError, match="GRAMPS_PASSWORD.*\\.env"):
        _connect(with_password(cfg, None), ask_secret)
    assert prompts == []


def test_connect_writer_without_a_user_names_both_places(cfg):
    ask_secret, _ = secret_script()
    with pytest.raises(ConfigError, match="gramps_write_user.*GRAMPS_WRITE_USER"):
        _connect_writer(cfg, ask_secret)


def test_connect_writer_prompts_for_its_own_password(cfg, on_terminal):
    ask_secret, prompts = secret_script()
    _connect_writer(replace(cfg, gramps_write_user="editor"), ask_secret)
    assert "Gramps write password for editor" in prompts[0]


def logged(cfg, summary="create Wren Pellow", transactions=(5, 6)):
    write_json(
        cfg.applied_path,
        {
            "steps": [
                {
                    "change": "c1",
                    "index": 0,
                    "summary": summary,
                    "created": [],
                    "updated": [],
                    "transactions": list(transactions),
                }
            ]
        },
    )


def test_undo_with_nothing_logged_writes_nothing(cfg):
    said: list[str] = []
    assert undo_applied(cfg, 1, input, None, said.append) == 0
    assert said == ["Nothing applied to undo."]


def test_undo_needs_the_typed_confirmation(cfg, monkeypatch):
    logged(cfg)
    monkeypatch.setattr(
        "geneanet_drift.wizard._connect_writer",
        lambda *args: pytest.fail("connected without confirmation"),
    )
    said: list[str] = []
    assert undo_applied(cfg, 1, lambda prompt: "yes", None, said.append) == 1
    assert "create Wren Pellow" in said[0]
    assert said[-1] == "Nothing written."
    assert read_log(cfg.applied_path)


@pytest.fixture
def accepted(cfg, monkeypatch):
    mine = tree(
        [gramps("h1", "Ardeth", "Pellow", citations=(crosswalk("Ardeth Pellow"),))]
    )
    people = his_people(his(1, "Ardeth", "Pellow", events=(his_event("born", 1902),)))
    changes = diff_people(
        people,
        mine,
        match_people(people, mine, SOURCE, load_negatives(cfg.negatives_path)),
    )
    save_decisions(cfg.decisions_path, {c.id: Decision(ACCEPTED) for c in changes})
    calls: list[str] = []
    monkeypatch.setattr("geneanet_drift.wizard._connect", lambda *a: "reader")
    monkeypatch.setattr("geneanet_drift.wizard.take_tree", lambda reader: mine)
    monkeypatch.setattr("geneanet_drift.wizard.load_tree", lambda pages, name: people)
    monkeypatch.setattr(
        "geneanet_drift.wizard.take_references", lambda reader: References({}, {})
    )
    monkeypatch.setattr(
        "geneanet_drift.wizard.backup",
        lambda reader, directory, now: calls.append("backup") or directory / "b",
    )
    monkeypatch.setattr("geneanet_drift.wizard._connect_writer", lambda *a: "writer")
    monkeypatch.setattr(
        "geneanet_drift.wizard.apply",
        lambda plan, writer, path, say: calls.append("apply") or len(plan.steps),
    )
    return calls


NOW = datetime(2026, 11, 8, 9, 0).astimezone()


def test_apply_shows_the_plan_then_backs_up_before_writing(cfg, tmp_path, accepted):
    said: list[str] = []

    wrong = apply_decisions(cfg, tmp_path, lambda p: "apply 0", None, said.append, NOW)
    assert (wrong, accepted) == (1, [])
    assert "steps, " in said[0]

    said.clear()
    assert (
        apply_decisions(cfg, tmp_path, lambda p: "apply 3", None, said.append, NOW) == 0
    )
    assert accepted == ["backup", "apply"]
    assert said[-1].startswith("Applied 3 steps")


def test_gedcom_is_a_listed_subcommand_that_passes_its_arguments_on(
    cfg, tmp_path, monkeypatch, capsys
):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "geneanet_drift.wizard.gedcom.main", lambda a: seen.append(a) or 0
    )
    assert main(["gedcom", "--pages", "P", "--out", "O"]) == 0
    assert main(["--data-dir", "D", "gedcom", "--pages", "P", "--out", "O"]) == 0
    assert seen == [
        ["--pages", "P", "--out", "O"],
        ["--data-dir", "D", "--pages", "P", "--out", "O"],
    ]
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "gedcom" in capsys.readouterr().out


@pytest.mark.parametrize("last", ["0", "-1", "x"])
def test_undo_rejects_a_step_count_below_one_at_parsing(last, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["undo", "--last", last])
    assert raised.value.code == 2
    assert "--last" in capsys.readouterr().err


def test_snapshot_saves_thumbnails_beside_the_snapshot_and_reports_both_counts(
    cfg, tmp_path, monkeypatch
):
    from geneanet_drift import wizard

    seen = []
    monkeypatch.setattr(
        wizard, "_connect", lambda *_: SimpleNamespace(role_warnings=list)
    )
    monkeypatch.setattr(wizard, "take_tree", lambda client: tree([]))
    monkeypatch.setattr(
        wizard,
        "save_thumbnails",
        lambda client, found, folder: seen.append(folder) or (3, 1),
    )
    out = tmp_path / "tree.json"
    said: list[str] = []
    assert wizard.snapshot_tree(cfg, out, None, said.append) == 0
    assert seen == [tmp_path / "thumbnails"]
    assert (tmp_path / "thumbnails").is_dir()
    assert any("3 saved, 1 failed" in line for line in said)
