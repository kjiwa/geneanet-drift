from dataclasses import replace
from datetime import date, datetime

import pytest

from geneanet_drift.apply import read_log
from geneanet_drift.config import (
    ConfigError,
    Run,
    State,
    end_of_day,
    load_run,
    load_state,
    save_run,
    save_state,
    write_json,
)
from geneanet_drift.decisions import ACCEPTED, Decision, save_decisions
from geneanet_drift.diff import diff_people
from geneanet_drift.gramps import GrampsClient, References
from geneanet_drift.match import load_negatives, load_pending
from geneanet_drift.match_tree import match_people
from geneanet_drift.wizard import (
    _connect,
    _connect_writer,
    apply_decisions,
    finish,
    main,
    run,
    undo_applied,
)
from tests.builders import SOURCE, crosswalk, gramps, his, his_event, his_people, tree
from tests.conftest import FakeGramps, make_client

TODAY = date(2026, 9, 20)
NEWEST = datetime(2026, 8, 25, 16, 10, 3)


class Interrupted(Exception):
    pass


class Script:
    """Stands in for the user: pastes the feed when asked and answers prompts in order."""

    def __init__(self, cfg, feed_text, answers=(), window="", confirm="", stop=False):
        self.cfg = cfg
        self.feed_text = feed_text
        self.answers = list(answers)
        self.window = window
        self.confirm = confirm
        self.stop = stop
        self.output: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "saved" in prompt:
            self.cfg.feed_path.write_text(self.feed_text)
            return ""
        if "How far back" in prompt:
            return self.window
        if "look right" in prompt:
            return self.confirm
        if self.stop and not self.answers:
            raise Interrupted
        return self.answers.pop(0)

    def say(self, line: str) -> None:
        self.output.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


@pytest.fixture
def synced(cfg):
    save_state(cfg.state_path, State(end_of_day(date(2026, 4, 2))))
    return cfg


@pytest.fixture
def unlinked():
    return make_client(FakeGramps(with_links=False))


def go(cfg, client: GrampsClient, script: Script, **options) -> int:
    return run(cfg, client, script.ask, script.say, TODAY, **options)


def test_linked_cluster_needs_no_questions_and_writes_a_review(
    synced, client, feed_text
):
    script = Script(synced, feed_text)
    assert go(synced, client, script) == 0
    assert "Gramps tree reachable: 3 people, 1 families." in script.text
    assert "Last sync: 2026-04-02 23:59:59." in script.text
    assert "STEP 2" not in script.text
    assert f"wrote {synced.review_path}, 4 changes" in script.text
    review = synced.review_path.read_text()
    assert "[I0001]" in review
    assert "## Informational" in review
    assert load_state(synced.state_path).last_sync == end_of_day(date(2026, 4, 2))


def test_step_one_echoes_what_was_parsed_and_asks_to_confirm(synced, client, feed_text):
    script = Script(synced, feed_text)
    go(synced, client, script)
    assert "Parsed 9 records, 2023-07-15 to 2026-08-25, newest first." in script.text
    assert "2026-08-25 16:10:03  Picture added" in script.text
    assert "2026-08-25 14:32:07  Individual updated" in script.text
    assert "Marden Quillon" in script.text
    assert "Looking after 2026-04-02 23:59:59 (change with --since): 6 records." in (
        script.text
    )
    assert any("Does that look right? [Y/n]" in p for p in script.prompts)
    assert not any("count" in p.casefold() for p in script.prompts)


def test_unrecognised_change_labels_are_flagged_in_the_echo(synced, client, feed_text):
    odd = feed_text.replace("Family added", "Note modified")
    script = Script(synced, odd)
    go(synced, client, script)
    assert "unrecognised change labels (see the review): ['Note modified']" in (
        script.text
    )


def test_declining_the_echo_records_nothing(synced, client, feed_text):
    script = Script(synced, feed_text, confirm="n")
    assert go(synced, client, script) == 1
    assert not synced.review_path.exists()
    assert load_state(synced.state_path).last_sync == end_of_day(date(2026, 4, 2))


def test_first_run_asks_how_far_back_and_defaults_to_2023_07_15(cfg, client, feed_text):
    script = Script(cfg, feed_text, answers=["n"])
    assert go(cfg, client, script) == 0
    assert "Looking back to 2023-07-15" in script.text
    assert "Looking back to 2023-07-15 (change with --since): 9 records." in script.text
    assert load_run(cfg.run_path).window_start == date(2023, 7, 15)
    assert "Changes after: 2023-07-14 23:59:59" in cfg.review_path.read_text()


def test_since_option_overrides_the_window_without_asking(cfg, client, feed_text):
    script = Script(cfg, feed_text)
    go(cfg, client, script, window=date(2026, 8, 25))
    assert not any("How far back" in p for p in script.prompts)
    assert "Looking back to 2026-08-25 (change with --since): 3 records." in script.text


def test_since_option_reaches_past_the_last_sync(synced, client, feed_text):
    script = Script(synced, feed_text, answers=["n"])
    go(synced, client, script, window=date(2023, 7, 15))
    assert "Looking back to 2023-07-15" in script.text


def test_oldest_row_newer_than_the_cursor_warns(synced, client, feed_text):
    save_state(synced.state_path, State(datetime(2020, 1, 1)))
    script = Script(synced, feed_text, answers=["n"])
    go(synced, client, script)
    assert "may not reach back far enough" in script.text
    quiet = Script(synced, feed_text)
    save_state(synced.state_path, State(datetime(2026, 4, 2)))
    go(synced, client, quiet)
    assert "may not reach back" not in quiet.text


def test_feed_option_reads_an_existing_file(synced, client, feed_text, tmp_path):
    saved = tmp_path / "elsewhere.txt"
    saved.write_text(feed_text)
    script = Script(synced, feed_text)
    assert go(synced, client, script, feed=saved) == 0
    assert not any("saved" in p for p in script.prompts)
    assert "wrote" in script.text


def test_second_run_after_done_has_nothing_to_do(synced, client, feed_text):
    go(synced, client, Script(synced, feed_text))
    assert finish(synced, lambda prompt: "y", lambda line: None) == 0
    after = Script(synced, feed_text)
    assert go(synced, client, after) == 0
    assert "Nothing to do." in after.text
    assert load_state(synced.state_path) == State(NEWEST)
    assert not synced.run_path.exists()


def test_an_edit_later_on_the_last_sync_day_is_not_dropped(synced, client, feed_text):
    save_state(synced.state_path, State(datetime(2026, 8, 25, 14, 25)))
    go(synced, client, Script(synced, feed_text, answers=["1"]))
    review = synced.review_path.read_text()
    assert "Update Tarik Zorvane" in review
    assert "Elowen" not in review


def test_done_warns_about_unticked_items_and_can_decline(synced, client, feed_text):
    go(synced, client, Script(synced, feed_text))
    assert finish(synced, lambda prompt: "n", lambda line: None) == 1
    assert load_state(synced.state_path).last_sync == end_of_day(date(2026, 4, 2))
    assert finish(synced, lambda prompt: "y", lambda line: None) == 0
    assert load_state(synced.state_path) == State(NEWEST)


def test_done_without_a_finished_run_says_so(synced):
    lines: list[str] = []
    assert finish(synced, lambda prompt: "y", lines.append) == 1
    save_run(synced.run_path, Run(accepted={"tariq zorvane": "I0001"}))
    assert finish(synced, lambda prompt: "y", lines.append) == 1
    assert synced.run_path.exists()


def test_accepting_one_probable_match_confirms_the_rest_of_its_cluster(
    synced, unlinked, feed_text
):
    script = Script(synced, feed_text, answers=["1"])
    go(synced, unlinked, script)
    assert "STEP 2 of 3" in script.text
    review = synced.review_path.read_text()
    assert "Update Tarik Zorvane [I0001]" in review
    assert "Update Elowen Quillon [I0002]" in review


def test_accepted_answers_are_saved_as_given_and_resume_after_an_interrupt(
    cfg, unlinked, feed_text
):
    first = Script(cfg, feed_text, answers=["1"], stop=True)
    with pytest.raises(Interrupted):
        go(cfg, unlinked, first)
    saved = load_run(cfg.run_path)
    assert saved.accepted == {"tariq zorvane": "I0001"}
    assert saved.window_start == date(2023, 7, 15)
    assert saved.next_sync is None

    resumed = Script(cfg, feed_text, answers=["n"])
    go(cfg, unlinked, resumed)
    assert not any("How far back" in p for p in resumed.prompts)
    assert resumed.answers == []
    assert "Update Tarik Zorvane [I0001]" in cfg.review_path.read_text()


def test_done_clears_the_saved_answers(cfg, unlinked, feed_text):
    go(cfg, unlinked, Script(cfg, feed_text, answers=["1", "n"]))
    assert load_run(cfg.run_path).accepted
    finish(cfg, lambda prompt: "y", lambda line: None)
    assert not cfg.run_path.exists()
    assert load_state(cfg.state_path).last_sync == NEWEST


def test_negative_answer_is_persisted_and_not_asked_again(synced, unlinked, feed_text):
    go(synced, unlinked, Script(synced, feed_text, answers=["n", "n"]))
    assert load_negatives(synced.negatives_path) == {
        ("tariq zorvane", "I0001"),
        ("elowen quillon", "I0002"),
    }
    assert "Add person Tariq Zorvane" in synced.review_path.read_text()


def test_pending_answer_resurfaces_next_run(synced, unlinked, feed_text):
    go(
        synced,
        unlinked,
        Script(synced, feed_text, answers=["p", "asked a cousin", "p", ""]),
    )
    assert {i.name: i.note for i in load_pending(synced.pending_path)} == {
        "Tariq Zorvane": "asked a cousin",
        "Elowen Quillon": "",
    }
    assert "Deferred" in synced.review_path.read_text()

    save_state(synced.state_path, State(NEWEST))
    again = Script(synced, feed_text, answers=["1"])
    go(synced, unlinked, again)
    assert "STEP 2 of 3" in again.text
    assert load_pending(synced.pending_path) == []


def test_notices_alone_still_write_a_review_and_record_the_sync(
    synced, client, feed_text
):
    save_state(synced.state_path, State(datetime(2026, 8, 25, 14, 40)))
    script = Script(synced, feed_text)
    assert go(synced, client, script) == 0
    assert "0 changes" in script.text
    assert "Nothing to apply; sync recorded." in script.text
    assert "a picture was added to Marden Quillon" in synced.review_path.read_text()
    assert load_state(synced.state_path).last_sync == NEWEST


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


def test_done_needs_no_credentials(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text(
        'tree = "example"\ngramps_url = "http://gramps.test"\n'
    )
    for variable in ("GRAMPS_USER", "GRAMPS_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert main(["--data-dir", str(data_dir), "--done"]) == 1
    assert "no finished run" in capsys.readouterr().out.casefold()


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


def test_apply_shows_the_plan_then_backs_up_before_writing(cfg, tmp_path, monkeypatch):
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
    said: list[str] = []
    now = datetime(2026, 11, 8, 9, 0).astimezone()

    wrong = apply_decisions(cfg, tmp_path, lambda p: "apply 0", None, said.append, now)
    assert (wrong, calls) == (1, [])
    assert "steps, " in said[0]

    said.clear()
    ask = lambda prompt: "apply 3"
    assert apply_decisions(cfg, tmp_path, ask, None, said.append, now) == 0
    assert calls == ["backup", "apply"]
    assert said[-1].startswith("Applied 3 steps")


@pytest.mark.parametrize("last", ["0", "-1", "x"])
def test_undo_rejects_a_step_count_below_one_at_parsing(last, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["undo", "--last", last])
    assert raised.value.code == 2
    assert "--last" in capsys.readouterr().err
