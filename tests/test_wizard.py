from datetime import date, datetime

import pytest

from geneanet_drift.config import (
    Run,
    State,
    end_of_day,
    load_run,
    load_state,
    save_run,
    save_state,
)
from geneanet_drift.gramps import GrampsClient
from geneanet_drift.match import load_negatives, load_pending
from geneanet_drift.wizard import finish, run
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
