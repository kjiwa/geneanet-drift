import argparse
import getpass
import os
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from geneanet_drift import gedcom
from geneanet_drift.apply import (
    Inputs,
    apply,
    backup,
    build_plan,
    newest_steps,
    preview,
    read_log,
    source_title,
    undo,
)
from geneanet_drift.capture import CaptureError, load_tree, page_times
from geneanet_drift.config import (
    Config,
    ConfigError,
    end_of_day,
    find_repo_root,
    load_config,
)
from geneanet_drift.decisions import load_decisions
from geneanet_drift.diff import diff_people
from geneanet_drift.feed import Entry, FeedError, since
from geneanet_drift.gramps import (
    GrampsClient,
    GrampsError,
    GrampsWriter,
    take_references,
    take_tree,
)
from geneanet_drift.match import load_negatives
from geneanet_drift.match_tree import match_people
from geneanet_drift.review_data import load_review
from geneanet_drift.server import serve
from geneanet_drift.status import (
    Visit,
    load_change_log,
    pages_to_visit,
)
from geneanet_drift.tree_file import write_tree

PAGES_PARENT = Path("Downloads") / "geneanet-drift"

Ask = Callable[[str], str]
Say = Callable[[str], None]
AskSecret = Callable[[str], str]


def _moment(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


def default_pages(tree: str) -> Path:
    return Path.home() / PAGES_PARENT / tree / "pages"


def _cutoff(since_day: date | None) -> datetime | None:
    return end_of_day(since_day - timedelta(days=1)) if since_day else None


def _report_visits(say: Say, visits: list[Visit]) -> None:
    if not visits:
        say("Nothing to visit: every changed page is captured.")
        return
    say(f"Visit these {len(visits)} pages in your browser:")
    for visit in visits:
        entry = visit.entry
        saved = f"saved {_moment(visit.saved)}" if visit.saved else "not captured"
        say("")
        say(f"  {entry.name}")
        say(f"    changed: {_moment(entry.when)}")
        say(f"    page:    {saved}")
        say(f"    {entry.url}")


def _report_next(say: Say, pages: Path) -> None:
    say("")
    say("Then run, in order, with DIR as")
    say(f"  {pages}")
    say("  geneanet-drift snapshot --out FILE")
    say("  geneanet-drift review --pages DIR \\")
    say("    --snapshot FILE")
    say("  geneanet-drift apply --pages DIR")
    say("  geneanet-drift gedcom --pages DIR \\")
    say("    --out FILE")


def _report_header(say: Say, entries: list[Entry], since_day: date | None) -> None:
    where = f"from {since_day} on" if since_day else "in the whole log"
    say(f"Changes {where}:")
    say(f"  {len(since(entries, _cutoff(since_day)))} of {len(entries)} log rows.")
    say("Times are compared as Geneanet wrote")
    say("them against local file times, so a")
    say("change within a few hours of a")
    say("capture may need a manual check.")
    say("")


def capture_status(
    cfg: Config, pages: Path, feed: Path | None, since_day: date | None, say: Say
) -> int:
    entries = load_change_log(pages, feed, cfg.tree)
    times = page_times(pages, cfg.tree, lambda note: say(f"Warning: {note}"))
    visits = pages_to_visit(entries, times, _cutoff(since_day))
    _report_header(say, entries, since_day)
    _report_visits(say, visits)
    _report_next(say, pages)
    return 0


def _login(
    cfg: Config,
    label: str,
    user: str | None,
    password: str | None,
    ask_secret: AskSecret,
) -> tuple[str, str]:
    prefix = label.upper().replace(" ", "_")
    if not user:
        raise ConfigError(
            f"no {label} user; set `{prefix.lower()}_user` in config.toml "
            f"or {prefix}_USER"
        )
    if not password:
        if not sys.stdin.isatty():
            raise ConfigError(
                f"{prefix}_PASSWORD is not set and there is no terminal to prompt on; "
                f"set it in the environment or in {cfg.data_dir / '.env'}"
            )
        password = ask_secret(f"{label} password for {user}: ")
        if not password:
            raise ConfigError(f"empty {label} password")
    return user, password


def _connect(cfg: Config, ask_secret: AskSecret) -> GrampsClient:
    user, password = _login(
        cfg, "Gramps", cfg.gramps_user, cfg.gramps_password, ask_secret
    )
    return GrampsClient(cfg.gramps_url, user, password)


def _connect_writer(cfg: Config, ask_secret: AskSecret) -> GrampsWriter:
    user, password = _login(
        cfg,
        "Gramps write",
        cfg.gramps_write_user,
        cfg.gramps_write_password,
        ask_secret,
    )
    return GrampsWriter(cfg.gramps_url, user, password)


def _confirmed(ask: Ask, say: Say, phrase: str) -> bool:
    if ask(f'Type "{phrase}" to continue: ').strip() == phrase:
        return True
    say("Nothing written.")
    return False


def apply_decisions(
    cfg: Config,
    pages: Path,
    ask: Ask,
    ask_secret: AskSecret,
    say: Say,
    now: datetime,
) -> int:
    reader = _connect(cfg, ask_secret)
    tree = take_tree(reader)
    people = load_tree(pages, cfg.tree)
    negatives = load_negatives(cfg.negatives_path)
    matches = match_people(people, tree, source_title(cfg.tree), negatives)
    inputs = Inputs(
        people,
        matches,
        tree,
        take_references(reader),
        cfg.tree,
        now.date(),
        pages.parent,
    )
    plan = build_plan(
        diff_people(people, tree, matches), load_decisions(cfg.decisions_path), inputs
    )
    logged = {(e["change"], e["index"]) for e in read_log(cfg.applied_path)}
    say(preview(plan, logged))
    pending = sum((s.change_id, s.index) not in logged for s in plan.steps)
    if not pending:
        say("Nothing to apply.")
        return 0
    if not _confirmed(ask, say, f"apply {pending}"):
        return 1
    say(f"Backup: {backup(reader, cfg.backups_dir, now)}")
    written = apply(plan, _connect_writer(cfg, ask_secret), cfg.applied_path, say)
    say(f"Applied {written} steps; see {cfg.applied_path}.")
    return 0


def undo_applied(
    cfg: Config, last: int, ask: Ask, ask_secret: AskSecret, say: Say
) -> int:
    entries = newest_steps(read_log(cfg.applied_path), last)
    if not entries:
        say("Nothing applied to undo.")
        return 0
    for entry in reversed(entries):
        say(f"  {entry['summary']}  (transactions {entry['transactions']})")
    if not _confirmed(ask, say, f"undo {len(entries)}"):
        return 1
    undone = undo(_connect_writer(cfg, ask_secret), cfg.applied_path, last, say)
    say(f"Undid {undone} steps.")
    return 0


def _check_snapshot_target(out: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve().parent)
    if repo_root is not None and out.resolve().is_relative_to(repo_root.resolve()):
        raise ConfigError(
            f"--out {out} is inside this tool's own git work tree ({repo_root})"
        )
    if not out.parent.is_dir():
        raise ConfigError(f"--out directory {out.parent} does not exist")


def snapshot_tree(cfg: Config, out: Path, ask_secret: AskSecret, say: Say) -> int:
    _check_snapshot_target(out)
    client = _connect(cfg, ask_secret)
    tree = take_tree(client)
    for warning in client.role_warnings():
        say(f"Warning: {warning}.")
    write_tree(out, tree)
    say(f"Wrote {len(tree.people)} people, {len(tree.families)} families to {out}.")
    return 0


def review_tree(cfg: Config, pages: Path, snapshot: Path, port: int, say: Say) -> int:
    if not pages.is_dir():
        raise ConfigError(f"--pages {pages} is not a directory")
    if not 0 <= port <= 65535:
        raise ConfigError(f"--port {port} is not a port number")
    return serve(cfg, load_review(cfg, pages, snapshot), port, say)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="geneanet-drift",
        description="Compare a captured Geneanet tree with a Gramps tree.",
    )
    parser.add_argument(
        "--data-dir", help="private data directory (default: GENEANET_DRIFT_DATA)"
    )
    commands = parser.add_subparsers(dest="command")
    status = commands.add_parser(
        "capture-status",
        help="list the pages to visit next (the default when no command is given)",
    )
    status.add_argument(
        "--pages", type=Path, help="captured pages (default: the extension's folder)"
    )
    status.add_argument(
        "--feed", type=Path, help="pasted change log, instead of the captured page"
    )
    status.add_argument(
        "--since",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="list only changes from this date on",
    )
    commands.add_parser(
        "gedcom",
        add_help=False,
        help="export the captured tree as GEDCOM (see `gedcom -h`)",
    )
    snapshot = commands.add_parser("snapshot", help="save the Gramps tree to a file")
    snapshot.add_argument("--out", type=Path, required=True, help="JSON file to write")
    review = commands.add_parser("review", help="review the captured tree in a browser")
    review.add_argument(
        "--pages", type=Path, required=True, help="directory of captured person pages"
    )
    review.add_argument(
        "--snapshot", type=Path, required=True, help="file from `snapshot --out`"
    )
    review.add_argument(
        "--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)"
    )
    apply_command = commands.add_parser(
        "apply", help="write accepted changes to Gramps"
    )
    apply_command.add_argument(
        "--pages", type=Path, required=True, help="captured pages"
    )
    undo_command = commands.add_parser("undo", help="revert the newest applied steps")
    undo_command.add_argument(
        "--last", type=_positive, required=True, help="steps to revert"
    )
    args, rest = parser.parse_known_args(argv)
    if args.command == "gedcom":
        carried = ["--data-dir", args.data_dir] if args.data_dir else []
        return gedcom.main([*carried, *rest])
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command is None:
        args = parser.parse_args([*argv, "capture-status"])
    try:
        cfg = load_config(args.data_dir, os.environ)
        for warning in cfg.warnings:
            print(f"Warning: {warning}.")
        if args.command == "snapshot":
            return snapshot_tree(cfg, args.out, getpass.getpass, print)
        if args.command == "review":
            return review_tree(cfg, args.pages, args.snapshot, args.port, print)
        if args.command == "apply":
            now = datetime.now().astimezone()
            return apply_decisions(cfg, args.pages, input, getpass.getpass, print, now)
        if args.command == "undo":
            return undo_applied(cfg, args.last, input, getpass.getpass, print)
        pages = args.pages or default_pages(cfg.tree)
        return capture_status(cfg, pages, args.feed, args.since, print)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except (ConfigError, FeedError, GrampsError, CaptureError, EOFError) as error:
        print(f"error: {error or type(error).__name__}", file=sys.stderr)
        return 1
