import argparse
import getpass
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from geneanet_drift.config import (
    Config,
    ConfigError,
    Run,
    State,
    end_of_day,
    load_config,
    load_run,
    load_state,
    save_run,
    save_state,
)
from geneanet_drift.feed import (
    Entry,
    FeedError,
    format_lifespan,
    history_url,
    parse_feed,
    since,
)
from geneanet_drift.gramps import GrampsClient, GrampsError, Snapshot, take_snapshot
from geneanet_drift.match import (
    Cluster,
    Resolution,
    WorkItem,
    build_clusters,
    build_notices,
    combine,
    load_negatives,
    load_pending,
    needing_decision,
    save_negatives,
    save_pending,
    work_items,
)
from geneanet_drift.review import render, source_title, unticked

DEFAULT_WINDOW = date(2023, 7, 15)
SELECT_ALL = "Cmd+A" if sys.platform == "darwin" else "Ctrl+A"
PREVIEW_ROWS = 3

Ask = Callable[[str], str]
Say = Callable[[str], None]
AskSecret = Callable[[str], str]


def _moment(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


def _report_status(say: Say, state: State, snapshot: Snapshot) -> None:
    last_sync = _moment(state.last_sync) if state.last_sync else "never"
    say(
        f"Last sync: {last_sync}.  Gramps tree reachable: "
        f"{len(snapshot.people)} people, {snapshot.families} families."
    )
    say("")


def _read_feed(cfg: Config, feed: Path | None, ask: Ask, say: Say) -> list[Entry]:
    say("STEP 1 of 3 - copy his change log")
    if feed is None:
        feed = cfg.feed_path
        feed.unlink(missing_ok=True)
        say(f"  a. Open {history_url(cfg.tree)}")
        say("     (English interface required; the parser reads English dates)")
        say('  b. Set "Results per page" to the largest value. Whether the page')
        say("     remembers that setting is unverified, so check it each time.")
        say(f"     Then select the whole page ({SELECT_ALL}) and copy it.")
        say(f"  c. Paste into {feed} and save.   (or: geneanet-drift --feed PATH)")
        ask("  Press Enter once the feed is saved: ")
    if not feed.is_file():
        raise FeedError(f"{feed} was not saved")
    return parse_feed(feed.read_text(), cfg.tree)


def _echo_feed(entries: list[Entry], say: Say) -> None:
    say("")
    say(
        f"  Parsed {len(entries)} records, {entries[-1].when.date()} to "
        f"{entries[0].when.date()}, newest first."
    )
    for entry in entries[:PREVIEW_ROWS]:
        say(f"    {_moment(entry.when)}  {entry.change:<20}  {entry.name}")
    unknown = sorted({e.change for e in entries if not e.known})
    if unknown:
        say(f"  Warning: unrecognised change labels (see the review): {unknown}")


def _ask_window(ask: Ask) -> date:
    while True:
        answer = ask(f"  How far back should I look? YYYY-MM-DD [{DEFAULT_WINDOW}]: ")
        if not answer.strip():
            return DEFAULT_WINDOW
        try:
            return date.fromisoformat(answer.strip())
        except ValueError:
            continue


def _window_start(
    state: State, run_state: Run, override: date | None, ask: Ask
) -> date | None:
    return (
        override
        or run_state.window_start
        or (None if state.last_sync else _ask_window(ask))
    )


def _cutoff(state: State, window: date | None) -> datetime:
    return end_of_day(window - timedelta(days=1)) if window else state.last_sync


def _report_window(
    say: Say, entries: list[Entry], cutoff: datetime, window: date | None
) -> None:
    where = f"back to {window}" if window else f"after {_moment(cutoff)}"
    fresh = len(since(entries, cutoff))
    say(f"  Looking {where} (change with --since): {fresh} records.")
    if entries[-1].when > cutoff:
        say(
            f"  Warning: the oldest row ({_moment(entries[-1].when)}) is newer than that; "
            'the page may not reach back far enough. Raise "Results per page".'
        )


def _step_one(
    cfg: Config,
    state: State,
    run_state: Run,
    feed: Path | None,
    override: date | None,
    ask: Ask,
    say: Say,
) -> tuple[list[Entry], datetime, Run] | None:
    entries = _read_feed(cfg, feed, ask, say)
    _echo_feed(entries, say)
    window = _window_start(state, run_state, override, ask)
    run_state = replace(run_state, window_start=window)
    save_run(cfg.run_path, run_state)
    cutoff = _cutoff(state, window)
    _report_window(say, entries, cutoff, window)
    if ask("  Does that look right? [Y/n] ").strip().casefold() == "n":
        save_run(cfg.run_path, replace(run_state, window_start=None))
        say("Stopped; nothing recorded. Fix the capture or the window and run again.")
        return None
    return entries, cutoff, run_state


def _next_state(state: State, entries: list[Entry]) -> State:
    newest = max(e.when for e in entries)
    return State(max(newest, state.last_sync) if state.last_sync else newest)


def _describe(resolution: Resolution, position: int, say: Say) -> None:
    item = resolution.item
    lifespan = f"   ({item.lifespan})" if item.lifespan else ""
    say("")
    say(f"  His tree: {item.name}{lifespan}   {item.url}&lang=en")
    say(f"  Changes:  {item.history}")
    for number, person in enumerate(resolution.candidates, start=1):
        years = format_lifespan(person.birth_year, person.death_year)
        shown = f"   ({years})" if years else ""
        say(f"  Gramps {number}: {person.name} [{person.gramps_id}]{shown}")
    say(f"  Why asked: {resolution.reason} ({position} left to decide)")


def _resolve(
    cfg: Config,
    items: list[WorkItem],
    snapshot: Snapshot,
    run_state: Run,
    ask: Ask,
    say: Say,
) -> tuple[list[Cluster], list[WorkItem], Run]:
    negatives = load_negatives(cfg.negatives_path)
    deferred: list[WorkItem] = []
    announced = False
    while True:
        deferred_keys = {d.key for d in deferred}
        live = [i for i in items if i.key not in deferred_keys]
        clusters = build_clusters(
            live, snapshot, negatives, run_state.accepted, cfg.priority_surnames
        )
        undecided = needing_decision(clusters)
        if not undecided:
            return clusters, deferred, run_state
        if not announced:
            say(
                "STEP 2 of 3 - confirm matches   (only for people not already linked in Gramps)"
            )
            announced = True
        current = undecided[0]
        _describe(current, len(undecided), say)
        choice = (
            ask("  Number to accept, n for none of these, p to decide later: ")
            .strip()
            .casefold()
        )
        candidates = current.candidates
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            accepted = {
                **run_state.accepted,
                current.item.key: candidates[int(choice) - 1].gramps_id,
            }
            run_state = replace(run_state, accepted=accepted)
            save_run(cfg.run_path, run_state)
        elif choice == "n":
            negatives.update((current.item.key, c.gramps_id) for c in candidates)
            save_negatives(cfg.negatives_path, negatives)
        elif choice == "p":
            note = ask("  Note for next time (e.g. who you asked): ").strip()
            deferred.append(replace(current.item, note=note))


def _record_sync(cfg: Config, next_state: State) -> None:
    save_state(cfg.state_path, next_state)
    cfg.run_path.unlink(missing_ok=True)


def run(
    cfg: Config,
    client: GrampsClient,
    ask: Ask,
    say: Say,
    today: date,
    feed: Path | None = None,
    window: date | None = None,
) -> int:
    state = load_state(cfg.state_path)
    snapshot = take_snapshot(client, source_title(cfg.tree))
    for warning in client.role_warnings():
        say(f"Warning: {warning}.")
    _report_status(say, state, snapshot)
    captured = _step_one(cfg, state, load_run(cfg.run_path), feed, window, ask, say)
    if captured is None:
        return 1
    entries, cutoff, run_state = captured
    next_state = _next_state(state, entries)

    recent = since(entries, cutoff)
    notices = build_notices(recent, snapshot)
    items = combine(work_items(recent), load_pending(cfg.pending_path))
    if not items and not notices:
        _record_sync(cfg, next_state)
        say("Nothing to do.")
        return 0

    clusters, deferred, run_state = _resolve(cfg, items, snapshot, run_state, ask, say)
    save_pending(cfg.pending_path, deferred)
    review = render(cfg.tree, clusters, notices, deferred, today, cutoff)
    cfg.review_path.write_text(review)
    changes = unticked(review)
    say("")
    say(f"STEP 3 of 3 - review   wrote {cfg.review_path}, {changes} changes")
    if not changes:
        _record_sync(cfg, next_state)
        say("  Nothing to apply; sync recorded.")
        return 0
    save_run(cfg.run_path, replace(run_state, next_sync=next_state.last_sync))
    say("  Apply in Desktop or Web, tick the boxes, then run `geneanet-drift --done`.")
    return 0


def finish(cfg: Config, ask: Ask, say: Say) -> int:
    run_state = load_run(cfg.run_path)
    if run_state.next_sync is None:
        say("No finished run to record; run `geneanet-drift` first.")
        return 1
    open_items = (
        unticked(cfg.review_path.read_text()) if cfg.review_path.is_file() else 0
    )
    if open_items:
        answer = ask(
            f"{open_items} items are unticked in {cfg.review_path}. Record sync anyway? [y/N] "
        )
        if answer.strip().casefold() != "y":
            say("Nothing recorded.")
            return 1
    _record_sync(cfg, State(run_state.next_sync))
    say(f"Recorded sync through {_moment(run_state.next_sync)}.")
    return 0


def _connect(cfg: Config, ask_secret: AskSecret) -> GrampsClient:
    if not cfg.gramps_user:
        raise ConfigError(
            "no Gramps user; set `gramps_user` in config.toml or GRAMPS_USER"
        )
    password = cfg.gramps_password
    if not password:
        if not sys.stdin.isatty():
            raise ConfigError(
                "GRAMPS_PASSWORD is not set and there is no terminal to prompt on; "
                f"set it in the environment or in {cfg.data_dir / '.env'}"
            )
        password = ask_secret(f"Gramps password for {cfg.gramps_user}: ")
        if not password:
            raise ConfigError("empty Gramps password")
    return GrampsClient(cfg.gramps_url, cfg.gramps_user, password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="geneanet-drift",
        description="Report a Geneanet tree's recent edits against a Gramps tree.",
    )
    parser.add_argument(
        "--data-dir", help="private data directory (default: GENEANET_DRIFT_DATA)"
    )
    parser.add_argument(
        "--done", action="store_true", help="record the sync after applying the review"
    )
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="review changes from this date on, instead of after the last sync",
    )
    parser.add_argument("--feed", type=Path, help="read the change log from this file")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.data_dir, os.environ)
        for warning in cfg.warnings:
            print(f"Warning: {warning}.")
        if args.done:
            return finish(cfg, input, print)
        client = _connect(cfg, getpass.getpass)
        today = datetime.now().astimezone().date()
        return run(cfg, client, input, print, today, args.feed, args.since)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except (ConfigError, FeedError, GrampsError, EOFError) as error:
        print(f"error: {error or type(error).__name__}", file=sys.stderr)
        return 1
