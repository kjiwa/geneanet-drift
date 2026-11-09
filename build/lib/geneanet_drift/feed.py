import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

GENEANET_HOST = "https://gw.geneanet.org"
TIMESTAMP_FORMAT = "%B %d, %Y %H:%M:%S"
DATE_LINE = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
TIME_LINE = re.compile(r"^\d\d:\d\d:\d\d$")
LIFESPAN_LINE = re.compile(r"^(?=.*\d)(\d{4})?\s*-\s*(\d{4})?$")
CHANGE_LINE = re.compile(r"^.+ (added|updated|deleted)$")
OCCURRENCE = re.compile(r"\.\d+$")
# A whole-page paste ends with Geneweb's legal notice after the last row.
FOOTER_LINE = re.compile(
    r"^[\d\s.]+$|^The Geneanet family trees are powered by Geneweb"
)
MAX_REPORTED = 3
HISTORY_ROW = "gw-history-editing-entry"

Line = tuple[int, str]  # 1-based line number in the pasted text, stripped text


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    when: datetime
    name: str
    birth_year: int | None
    death_year: int | None
    change: str
    admin: str
    url: str
    address: tuple[str, str, str] | None = None  # (n, p, oc) of the row link


def tree_url(tree: str) -> str:
    return f"{GENEANET_HOST}/{tree}_w"


def history_url(tree: str) -> str:
    return f"{tree_url(tree)}/history/editing?lang=en"


def person_url(tree: str, name: str) -> str:
    *given, surname = name.split()
    query = urlencode(
        {"n": surname.casefold(), "p": " ".join(given).casefold(), "oc": 0}
    )
    return f"{tree_url(tree)}?{query}"


def name_key(name: str) -> str:
    return " ".join(name.casefold().split())


def is_stale(name: str) -> bool:
    # Geneweb prints the raw key (`given.occ surname`) unlinked when a history
    # row's person has since been renamed, so no page exists to link to.
    return any(OCCURRENCE.search(token) for token in name.split())


def _raise_if_any(problems: list[str]) -> None:
    if not problems:
        return
    more = len(problems) - MAX_REPORTED
    suffix = f" (and {more} more)" if more > 0 else ""
    shown = "; ".join(problems[:MAX_REPORTED])
    raise FeedError(f"the pasted change log did not parse: {shown}{suffix}")


def _numbered(text: str) -> list[Line]:
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]


def _record_starts(lines: list[Line]) -> list[int]:
    starts: list[int] = []
    problems: list[str] = []
    for index, (number, text) in enumerate(lines):
        if not DATE_LINE.match(text):
            continue
        if index + 1 < len(lines) and TIME_LINE.match(lines[index + 1][1]):
            starts.append(index)
        else:
            problems.append(f"line {number}: {text!r} is not followed by a time")
    _raise_if_any(problems)
    return starts


def _cut_footer(body: list[Line]) -> list[Line]:
    end = next(
        (index for index, (_, text) in enumerate(body) if FOOTER_LINE.match(text)),
        len(body),
    )
    return body[:end]


def _parse_when(date_line: Line, time_line: Line) -> datetime:
    try:
        return datetime.strptime(f"{date_line[1]} {time_line[1]}", TIMESTAMP_FORMAT)
    except ValueError as error:
        raise FeedError(f"line {date_line[0]}: not a date: {date_line[1]!r}") from error


def _parse_lifespan(text: str) -> tuple[int | None, int | None]:
    birth, death = LIFESPAN_LINE.match(text).groups()
    return (int(birth) if birth else None, int(death) if death else None)


def _split_body(body: list[Line]) -> tuple[Line, Line | None, Line, str]:
    person, *rest = body
    lifespan = rest.pop(0) if rest and LIFESPAN_LINE.match(rest[0][1]) else None
    # An unrecognised change label is still the first line after the person.
    change_at = next(
        (i for i, (_, text) in enumerate(rest) if CHANGE_LINE.match(text)), 0
    )
    if not rest or change_at != 0 or len(rest) > 2:
        shown = " | ".join(text for _, text in rest)
        raise FeedError(
            f"line {person[0]}: expected a change line then an optional admin "
            f"after {person[1]!r}, got: {shown!r}"
        )
    admin = rest[1][1] if len(rest) == 2 else ""
    return person, lifespan, rest[0], admin


def _parse_record(record: list[Line], tree: str) -> Entry:
    when = _parse_when(record[0], record[1])
    body = _cut_footer(record[2:])
    if not body:
        raise FeedError(f"line {record[0][0]}: record has no person")
    person, lifespan, change, admin = _split_body(body)
    birth, death = _parse_lifespan(lifespan[1]) if lifespan else (None, None)
    url = "" if is_stale(person[1]) else person_url(tree, person[1])
    return Entry(when, person[1], birth, death, change[1], admin, url)


def _check_newest_first(entries: list[Entry], numbers: list[int]) -> None:
    _raise_if_any(
        [
            f"line {number}: record is newer than the one before it; expected newest first"
            for number, newer, older in zip(numbers[1:], entries[1:], entries)
            if newer.when > older.when
        ]
    )


def parse_feed(text: str, tree: str) -> list[Entry]:
    lines = _numbered(text)
    starts = _record_starts(lines)
    if not starts:
        raise FeedError("no change-log rows found; was the page in English (?lang=en)?")
    ends = [*starts[1:], len(lines)]
    entries: list[Entry] = []
    problems: list[str] = []
    for start, end in zip(starts, ends):
        try:
            entries.append(_parse_record(lines[start:end], tree))
        except FeedError as error:
            problems.append(str(error))
    _raise_if_any(problems)
    _check_newest_first(entries, [lines[start][0] for start in starts])
    return entries


class _HistoryRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[int, str | None, list[Line]]] = []
        self.inside = False

    def handle_starttag(self, tag, attrs):
        if tag == HISTORY_ROW:
            self.rows.append((self.getpos()[0], None, []))
            self.inside = True
        elif tag == "a" and self.inside and self.rows[-1][1] is None:
            href = dict(attrs).get("href")
            if href:
                start, _, texts = self.rows[-1]
                self.rows[-1] = (start, href, texts)

    def handle_endtag(self, tag):
        if tag == HISTORY_ROW:
            self.inside = False

    def handle_data(self, data):
        if self.inside and data.strip():
            self.rows[-1][2].append((self.getpos()[0], data.strip()))


def _link_address(href: str, tree: str) -> tuple[str, str, str] | None:
    parts = urlsplit(href)
    query = parse_qs(parts.query, keep_blank_values=True)
    if parts.path != f"/{tree}_w" or "m" in query or not {"n", "p"} <= query.keys():
        return None
    return (query["n"][0], query["p"][0], query.get("oc", ["0"])[0] or "0")


def _history_entry(row: list[Line], href: str, address) -> Entry:
    if len(row) < 5:
        raise FeedError(
            f"line {row[0][0] if row else '?'}: change-log row is incomplete"
        )
    when = _parse_when(row[0], row[1])
    person, lifespan, change, admin = _split_body(row[2:])
    birth, death = _parse_lifespan(lifespan[1]) if lifespan else (None, None)
    url = urljoin(GENEANET_HOST, href)
    return Entry(when, person[1], birth, death, change[1], admin, url, address)


def parse_history_html(html: str, tree: str) -> list[Entry]:
    parser = _HistoryRows()
    parser.feed(html)
    parser.close()
    if not parser.rows:
        raise FeedError("no change-log rows found; was the page in English (?lang=en)?")
    entries: list[Entry] = []
    numbers: list[int] = []
    for start, href, texts in parser.rows:
        # Unlinked rows are people renamed since; no page exists to match them to.
        address = _link_address(href, tree) if href else None
        if address is None:
            continue
        entries.append(_history_entry(texts, href, address))
        numbers.append(start)
    if not entries:
        raise FeedError(
            f"{len(parser.rows)} change-log rows found but none links to a person of {tree!r}"
        )
    _check_newest_first(entries, numbers)
    return entries


def since(entries: list[Entry], cutoff: datetime | None) -> list[Entry]:
    return [e for e in entries if cutoff is None or e.when > cutoff]
