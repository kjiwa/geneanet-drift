import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
DATE = re.compile(
    rf"(?P<text>(?:(?P<modifier>about|possibly|before) )?(?:(?P<in>in) )?"
    rf"(?:(?P<month>{'|'.join(MONTHS)})(?: (?P<day>\d{{1,2}}))?, )?"
    rf"(?P<year>\d{{4}}))(?: \((?:{WEEKDAYS})\))?(?![\w])"
)
AGE = r"(?:\d+|one) (?:years?|months?|days?) old"
AGED = re.compile(rf",?\s*aged (?:about )?{AGE}$")
AGE_LINE = re.compile(rf"Age: {AGE}")
EVENT_LINE = re.compile(r"(Born|Deceased|Buried|Killed)(?: (.*))?$", re.DOTALL)
SOURCE_LINE = re.compile(r"([^:]+): (.+)$", re.DOTALL)
VOID_TAGS = frozenset({"br", "img", "link", "meta", "input", "hr", "base", "col"})
SKIPPED_SECTIONS = frozenset(
    {
        "Siblings",
        "Half-siblings",
        "Family tree (up to grandparents)",
        "Family Tree Preview",
        "Photos and archival documents",
        "Guided tour of the family tree's menus",
        "Add picture",
    }
)
UNION_SECTIONS = ("Spouses and children", "Spouses")
SEX_LABELS = {"Male": "male", "Female": "female", "Unknown gender": "unknown"}
UNION_KINDS = {"Married to": "married", "Relationship with": "relationship"}
MAX_REPORTED = 3


class CaptureError(ValueError):
    pass


class Address(NamedTuple):
    surname: str
    given: str
    occurrence: int


@dataclass(frozen=True)
class Ref:
    key: int | None
    address: Address | None
    placeholder: bool = False


@dataclass(frozen=True)
class Date:
    text: str
    year: int
    month: int | None
    day: int | None
    modifier: str | None
    quality: str | None


@dataclass(frozen=True)
class Event:
    kind: str
    date: Date | None
    place: str | None


@dataclass(frozen=True)
class Union:
    key: int
    kind: str
    spouse: Ref
    date: Date | None
    place: str | None
    children: tuple[Ref, ...]
    notes: tuple["Note", ...] = ()


@dataclass(frozen=True)
class Media:
    id: int
    title: str
    date: str | None
    original: str
    thumbnail: str
    is_default: bool


@dataclass(frozen=True)
class Note:
    kind: str
    text: str


@dataclass(frozen=True)
class Source:
    fact: str
    text: str


@dataclass(frozen=True)
class Person:
    key: int
    given: str
    surname: str
    public_name: str | None
    other_names: tuple[str, ...]
    sex: str
    occupation: str | None
    events: tuple[Event, ...]
    parents: tuple[Ref, ...]
    unions: tuple[Union, ...]
    notes: tuple[Note, ...]
    sources: tuple[Source, ...]
    media: tuple[Media, ...]
    address: Address | None


class Node:
    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def elements(self) -> Iterator["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.elements()

    def child_elements(self, tag: str | None = None) -> list["Node"]:
        return [
            c
            for c in self.children
            if isinstance(c, Node) and (tag is None or c.tag == tag)
        ]

    def text(self) -> str:
        inner = "".join(c if isinstance(c, str) else c.text() for c in self.children)
        if self.tag == "br":
            return "\n"
        return f"\n{inner}\n" if self.tag == "p" else inner

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse_dom(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _query(href: str) -> dict[str, str]:
    parsed = parse_qs(urlsplit(href).query, keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items()}


def _tree_link(node: Node, tree: str) -> dict[str, str] | None:
    if node.tag != "a" or "href" not in node.attrs:
        return None
    path = urlsplit(node.attrs["href"].strip()).path
    if path.rsplit("/", 1)[-1] != f"{tree}_w":
        return None
    return _query(node.attrs["href"].strip())


def _address(query: dict[str, str]) -> Address | None:
    if not query.get("n") and not query.get("p"):
        return None
    return Address(query.get("n", ""), query.get("p", ""), int(query.get("oc") or 0))


def _person_ref(node: Node, tree: str) -> Ref | None:
    query = _tree_link(node, tree)
    if query is None or "m" in query:
        return None
    if "i" not in query and "p" not in query:
        return None
    return Ref(
        int(query["i"]) if query.get("i") else None,
        _address(query),
        placeholder=set(_clean(node.text()).replace(" ", "")) == {"?"},
    )


def _refs(node: Node, tree: str) -> tuple[Ref, ...]:
    return tuple(r for a in node.elements() if (r := _person_ref(a, tree)))


def _parse_date(text: str) -> tuple[Date | None, str]:
    match = DATE.match(text)
    if not match:
        return None, text
    month, day = match["month"], match["day"]
    bare = not match["in"] and not match["modifier"]
    if (bare and not day) or (match["in"] and day):
        raise CaptureError(f"unknown date form: {text!r}")
    modifier = match["modifier"]
    date = Date(
        text=match["text"],
        year=int(match["year"]),
        month=MONTHS.index(month) + 1 if month else None,
        day=int(day) if day else None,
        modifier=None if modifier == "possibly" else modifier,
        quality="estimated" if modifier == "possibly" else None,
    )
    return date, text[match.end() :]


def _clean_place(text: str) -> str | None:
    return text.strip(" ,") or None


def _event_place(rest: str, text: str) -> str | None:
    if rest and not rest.startswith("- "):
        raise CaptureError(f"unreadable event text: {text!r}")
    return _clean_place(rest[2:])


def _union_place(rest: str, text: str) -> str | None:
    if rest and not rest.startswith(","):
        raise CaptureError(f"unreadable marriage text: {text!r}")
    return _clean_place(rest)


def _parse_event(match: re.Match) -> Event:
    body = AGED.sub("", _clean(match[2] or ""))
    date, rest = _parse_date(body)
    return Event(match[1].lower(), date, _event_place(rest.strip(), body))


def _find_h1(root: Node) -> Node:
    h1 = next((e for e in root.elements() if e.tag == "h1"), None)
    if h1 is None:
        raise CaptureError("no h1 name heading")
    return h1


def _h1_key(h1: Node, tree: str) -> int:
    for link in h1.elements():
        query = _tree_link(link, tree)
        if query and query.get("m") == "MOD_IND" and query.get("i", "").isdigit():
            return int(query["i"])
    raise CaptureError("h1 has no MOD_IND key")


def _name_links(nodes: list[Node], tree: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in nodes:
        query = _tree_link(node, tree)
        if query and query.get("m") in ("P", "N"):
            names[query["m"]] = _clean(node.text())
    return names


def _header_ems(header: list[Node]) -> list[Node]:
    ems: list[Node] = []
    for node in header:
        if node.tag == "ul":
            break
        if node.tag == "em":
            ems.append(node)
    return ems


def _h1_sex(h1: Node) -> str:
    for image in h1.elements():
        sex = SEX_LABELS.get(image.attrs.get("alt", ""))
        if image.tag == "img" and sex:
            return sex
    raise CaptureError("h1 has no sex image")


def _canonical(root: Node) -> Address | None:
    for link in root.elements():
        if link.tag == "link" and link.attrs.get("rel") == "canonical":
            return _address(_query(link.attrs.get("href", "")))
    raise CaptureError("no canonical link")


def _sections(
    order: list[Node], h1: Node
) -> tuple[list[Node], list[tuple[str, list[Node]]]]:
    start = order.index(h1)
    header: list[Node] = []
    sections: list[tuple[str, list[Node]]] = []
    for node in order[start + 1 :]:
        if node.tag == "h2":
            sections.append((_clean(node.text()), []))
        elif sections:
            sections[-1][1].append(node)
        else:
            header.append(node)
    return header, sections


def _header_list(header: list[Node]) -> Node | None:
    return next((n for n in header if n.tag == "ul"), None)


def _header_lines(ul: Node | None) -> tuple[list[Event], str | None]:
    events: list[Event] = []
    free: list[str] = []
    for item in ul.child_elements("li") if ul else []:
        line = _clean(item.text())
        match = EVENT_LINE.match(line)
        if match:
            events.append(_parse_event(match))
        elif not AGE_LINE.fullmatch(line):
            free.append(line)
    if len(free) > 1:
        raise CaptureError("more than one free-text header line")
    return events, free[0] if free else None


def _first_list(nodes: list[Node]) -> Node | None:
    return next((n for n in nodes if n.tag == "ul"), None)


def _parse_parents(nodes: list[Node], tree: str) -> tuple[Ref, ...]:
    ul = _first_list(nodes)
    return _refs(ul, tree) if ul else ()


def _union_children(item: Node, tree: str) -> tuple[Ref, ...]:
    lists = item.child_elements("ul")
    return _refs(lists[0], tree) if lists else ()


def _is_family_link(node: Node, tree: str) -> bool:
    query = _tree_link(node, tree)
    return bool(query and query.get("m") == "MOD_FAM" and query.get("i", "").isdigit())


def _parse_union(item: Node, tree: str) -> Union:
    key = spouse = None
    lead: list[str] = []
    em_text = ""
    for child in item.children:
        if spouse is not None:
            break
        if isinstance(child, str):
            lead.append(child)
        elif child.tag == "em":
            em_text = _clean(child.text())
        elif _is_family_link(child, tree):
            key = int(_tree_link(child, tree)["i"])
        else:
            spouse = _person_ref(child, tree)
    if key is None or spouse is None:
        raise CaptureError("union without family key or spouse link")
    lead_text = _clean(" ".join(lead))
    if lead_text not in UNION_KINDS:
        raise CaptureError(f"unknown union text: {lead_text!r}")
    date, rest = _parse_date(em_text)
    place = _union_place(rest.strip(), em_text) if date else _clean_place(rest)
    return Union(
        key, UNION_KINDS[lead_text], spouse, date, place, _union_children(item, tree)
    )


def _parse_unions(nodes: list[Node], tree: str) -> tuple[Union, ...]:
    ul = _first_list(nodes)
    if ul is None:
        return ()
    return tuple(_parse_union(li, tree) for li in ul.child_elements("li"))


def _parse_notes(nodes: list[Node], body_class: str) -> tuple[Note, ...]:
    notes: list[Note] = []
    kind = ""
    for node in nodes:
        if node.tag == "h3" and node.has_class("note_type"):
            kind = _clean(node.text())
        elif node.tag == "div" and node.has_class(body_class):
            notes.append(Note(kind, _note_text(node)))
    return tuple(notes)


def _note_text(node: Node) -> str:
    lines = (_clean(line) for line in node.text().split("\n"))
    return "\n".join(line for line in lines if line)


def _attach_family_notes(unions: list[Union], notes: tuple[Note, ...]) -> list[Union]:
    if not notes:
        return unions
    if len(unions) != 1:
        raise CaptureError(f"family note on a page with {len(unions)} unions")
    return [replace(unions[0], notes=notes)]


def _parse_sources(nodes: list[Node]) -> tuple[Source, ...]:
    sources: list[Source] = []
    for node in nodes:
        if node.tag == "ul" and node.has_class("fiche-source-ind"):
            sources.extend(_source(li) for li in node.child_elements("li"))
    return tuple(sources)


def _source(item: Node) -> Source:
    line = _clean(item.text())
    match = SOURCE_LINE.match(line)
    if not match:
        raise CaptureError(f"unreadable source line: {line!r}")
    return Source(match[1], match[2])


def _media_lists(script: str) -> Iterator[list]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"media"\s*:\s*(?=\[)', script):
        try:
            value, _ = decoder.raw_decode(script, match.end())
        except ValueError as error:
            raise CaptureError(f"unreadable media JSON: {error}") from error
        yield value


def _media(item: dict) -> Media:
    images = item["doc_images"]
    return Media(
        id=item["doc_id"],
        title=item.get("doc_title") or "",
        date=item.get("doc_date"),
        original=images["original"],
        thumbnail=images["thumbnail"],
        is_default=bool(item.get("is_default")),
    )


def _parse_media(root: Node) -> tuple[Media, ...]:
    found: list[Media] = []
    for script in root.elements():
        text = script.text() if script.tag == "script" else ""
        for items in _media_lists(text):
            found.extend(_media(i) for i in items if "doc_id" in i)
    return tuple({m.id: m for m in found}.values())


def _person_sections(sections: list[tuple[str, list[Node]]]) -> dict[str, list[Node]]:
    found: dict[str, list[Node]] = {}
    for title, nodes in sections:
        name = re.sub(r"\s+", " ", title)
        if name in SKIPPED_SECTIONS:
            continue
        if name not in {"Parents", "Notes", "Sources", "Family Note", *UNION_SECTIONS}:
            raise CaptureError(f"unknown section {name!r}")
        found.setdefault(name, []).extend(nodes)
    return found


def parse_person(html: str, tree: str) -> Person:
    root = _parse_dom(html)
    h1 = _find_h1(root)
    header, sections = _sections(list(root.elements()), h1)
    found = _person_sections(sections)
    events, occupation = _header_lines(_header_list(header))
    names = _name_links(list(h1.elements()), tree)
    ems = _header_ems(header)
    if "P" in names:
        public_name = None
    else:
        names = _name_links([n for em in ems for n in em.elements()], tree)
        public_name = _clean(h1.text()) or None
        ems = [em for em in ems if not _name_links(list(em.elements()), tree)]
    if "P" not in names:
        raise CaptureError("no given name link")
    unions = _attach_family_notes(
        [
            u
            for name in UNION_SECTIONS
            for u in _parse_unions(found.get(name, []), tree)
        ],
        _parse_notes(found.get("Family Note", []), "fiche-note-union"),
    )
    return Person(
        key=_h1_key(h1, tree),
        given=names["P"],
        surname=names.get("N", ""),
        public_name=public_name,
        other_names=tuple(_clean(em.text()).strip("()").strip() for em in ems),
        sex=_h1_sex(h1),
        occupation=occupation,
        events=tuple(events),
        parents=_parse_parents(found.get("Parents", []), tree),
        unions=tuple(unions),
        notes=_parse_notes(found.get("Notes", []), "fiche-note-ind"),
        sources=_parse_sources(found.get("Sources", [])),
        media=_parse_media(root),
        address=_canonical(root),
    )


def _related(person: Person) -> Iterator[Ref]:
    yield from person.parents
    for union in person.unions:
        yield union.spouse
        yield from union.children


def _index_addresses(people: dict[int, Person]) -> dict[Address, list[int]]:
    index: dict[Address, list[int]] = {}
    for key, person in people.items():
        if person.address:
            index.setdefault(person.address, []).append(key)
    return index


def _links_back(candidate: Person, referrer: Person) -> bool:
    return any(
        ref.key == referrer.key
        or (referrer.address is not None and ref.address == referrer.address)
        for ref in _related(candidate)
    )


def _resolve_ref(
    ref: Ref,
    referrer: Person,
    people: dict[int, Person],
    index: dict[Address, list[int]],
) -> int | None:
    if ref.key is not None:
        return ref.key if ref.key in people or ref.placeholder else None
    candidates = index.get(ref.address, [])
    if len(candidates) > 1:
        candidates = [k for k in candidates if _links_back(people[k], referrer)]
    return candidates[0] if len(candidates) == 1 else None


def _link(ref: Ref, referrer, people, index, unresolved: list[str]) -> Ref:
    key = _resolve_ref(ref, referrer, people, index)
    if key is None:
        unresolved.append(f"person-{referrer.key}: {ref.address or ref.key}")
    return replace(ref, key=key)


def _link_person(person, people, index, unresolved: list[str]) -> Person:
    def link(ref: Ref) -> Ref:
        return _link(ref, person, people, index, unresolved)

    unions = tuple(
        replace(
            u,
            spouse=link(u.spouse),
            children=tuple(link(c) for c in u.children),
        )
        for u in person.unions
    )
    return replace(
        person, parents=tuple(link(r) for r in person.parents), unions=unions
    )


def _page_number(path: Path) -> int:
    return int(path.stem.removeprefix("person-"))


def _page_paths(pages_dir: Path) -> list[Path]:
    paths = pages_dir.glob("person-*.html")
    numbered = [p for p in paths if p.stem.removeprefix("person-").isdigit()]
    return sorted(numbered, key=_page_number)


def _parse_pages(pages_dir: Path, tree: str) -> tuple[dict[int, Person], list[str]]:
    people: dict[int, Person] = {}
    problems: list[str] = []
    for path in _page_paths(pages_dir):
        try:
            person = parse_person(path.read_text(encoding="utf-8"), tree)
        except (CaptureError, KeyError, TypeError, ValueError) as error:
            problems.append(
                f"{path.name}: {error!r}"
                if not isinstance(error, CaptureError)
                else f"{path.name}: {error}"
            )
            continue
        if person.key in people:
            problems.append(f"{path.name}: duplicate key {person.key}")
            continue
        people[person.key] = person
    return people, problems


def _raise_if_any(problems: list[str]) -> None:
    if not problems:
        return
    shown = "; ".join(problems[:MAX_REPORTED])
    more = len(problems) - MAX_REPORTED
    suffix = f" (and {more} more)" if more > 0 else ""
    raise CaptureError(f"{len(problems)} capture problems: {shown}{suffix}")


def load_tree(pages_dir: Path, tree: str) -> dict[int, Person]:
    people, problems = _parse_pages(pages_dir, tree)
    _raise_if_any(problems)
    index = _index_addresses(people)
    unresolved: list[str] = []
    linked = {
        key: _link_person(person, people, index, unresolved)
        for key, person in people.items()
    }
    _raise_if_any(unresolved)
    return linked
