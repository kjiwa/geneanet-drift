from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from geneanet_drift.capture import Address, address_from_url
from geneanet_drift.feed import (
    Entry,
    FeedError,
    history_url,
    parse_feed,
    parse_history_html,
    since,
)

HISTORY_PAGE = "history-editing.html"


@dataclass(frozen=True)
class Visit:
    entry: Entry  # the person's newest change
    saved: datetime | None  # when their captured page was saved, if there is one


def has_history_page(pages: Path) -> bool:
    return (pages / HISTORY_PAGE).is_file()


def load_change_log(pages: Path, feed: Path | None, tree: str) -> list[Entry]:
    if feed is not None:
        if not feed.is_file():
            raise FeedError(f"{feed} is not a file")
        return parse_feed(feed.read_text(), tree)
    if not has_history_page(pages):
        raise FeedError(
            f"no {HISTORY_PAGE} in {pages}; open {history_url(tree)} with the "
            "extension on, or paste that page into a file and pass --feed FILE"
        )
    return parse_history_html((pages / HISTORY_PAGE).read_text(encoding="utf-8"), tree)


def entry_address(entry: Entry) -> Address | None:
    try:
        if entry.address:
            surname, given, occurrence = entry.address
            return Address(surname, given, int(occurrence))
        return address_from_url(entry.url)
    except ValueError as error:
        raise FeedError(
            f"{entry.name}: the occurrence number in its link is not a number"
        ) from error


def pages_to_visit(
    entries: list[Entry], times: dict[Address, datetime], cutoff: datetime | None
) -> list[Visit]:
    newest: dict[Address, Entry] = {}
    for entry in since(entries, cutoff):
        address = entry_address(entry)
        if address is not None:
            newest.setdefault(address, entry)
    return [
        Visit(entry, times.get(address))
        for address, entry in newest.items()
        if address not in times or times[address] < entry.when
    ]
