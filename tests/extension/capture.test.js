"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const capture = require("../../extension/lib/capture.js");

const BASE = "https://gw.geneanet.org/exampletree_w";
const read = (name) =>
  fs.readFileSync(path.join(__dirname, "..", "fixtures", "geneanet", `${name}.html`), "utf8");

const PERSON_PAGES = [
  ["person_rich", 6],
  ["person_two_unions", 10],
  ["person_sparse", 408],
  ["person_source", 5],
  ["person_occurrence", 77],
];

for (const [name, key] of PERSON_PAGES) {
  test(`classify ${name} as person ${key}`, () => {
    const html = read(name);
    assert.deepEqual(capture.classify(`${BASE}?lang=en&n=x&p=y`, html), { kind: "person", key: String(key) });
    assert.equal(capture.personKeyFromHtml(html), String(key));
  });
}

test("classify history and roster", () => {
  assert.deepEqual(capture.classify(`${BASE}/history/editing`, read("history_editing")), {
    kind: "history",
    key: null,
  });
  assert.deepEqual(capture.classify(`${BASE}?lang=en&m=LIST_IND&pg=1&sz=500`, read("list_ind")), {
    kind: "roster",
    key: null,
  });
});

test("history landing page is history only once rows exist", () => {
  assert.equal(capture.classify(`${BASE}/history`, "<html></html>"), null);
  assert.equal(capture.classify(`${BASE}/history`, read("history_editing")).kind, "history");
});

test("classify ignores forms and unknown pages", () => {
  const html = read("person_rich");
  assert.equal(capture.classify(`${BASE}?lang=en&m=MOD_IND&i=6`, html), null);
  assert.equal(capture.classify(`${BASE}?lang=en&m=MOD_FAM&i=1&ip=6`, html), null);
  assert.equal(capture.classify(`${BASE}?lang=en&m=S&v=ashby`, "<html><body></body></html>"), null);
  const form = html.replace("</body>", '<input type="hidden" name="digest" value="abc"></body>');
  assert.equal(capture.classify(`${BASE}?n=a&p=b`, form), null);
});

test("treeOf reads the tree from the path", () => {
  assert.equal(capture.treeOf(`${BASE}?n=a`), "exampletree");
  assert.equal(capture.treeOf(`${BASE}/history/editing`), "exampletree");
  assert.equal(capture.treeOf("https://gw.geneanet.org/other_w?n=a"), "other");
  assert.equal(capture.treeOf("https://gw.geneanet.org/"), null);
});

test("parseQuery decodes entities and escapes", () => {
  assert.deepEqual(capture.parseQuery("x_w?lang=en&amp;n=a%20b&p=c+d&oc=1"), {
    lang: "en",
    n: "a b",
    p: "c d",
    oc: "1",
  });
});

test("parseQuery keeps a malformed escape instead of throwing", () => {
  assert.deepEqual(capture.parseQuery("x_w?n=a%zz&p=b"), { n: "a%zz", p: "b" });
});

test("history path accepts a trailing slash", () => {
  const url = "https://gw.geneanet.org/exampletree_w/history/editing/";
  assert.equal(capture.classify(url, read("history_editing")).kind, "history");
});

test("toVisit treats a date-only review date as the end of that day", () => {
  const history = [
    { n: "a", p: "x", oc: "0", at: "2026-10-01T09:00:00.000Z" },
    { n: "b", p: "y", oc: "0", at: "2026-10-02T09:00:00.000Z" },
  ];
  const result = capture.toVisit({ captures: {} }, [], history, "2026-10-01");
  assert.deepEqual(result.map((link) => link.n), ["b"]);
});

test("personIdentity and personName", () => {
  const html = read("person_occurrence");
  assert.deepEqual(capture.personIdentity(`${BASE}?lang=en&n=quill&p=sam&oc=1`, html), {
    n: "quill",
    p: "sam",
    oc: "1",
  });
  assert.deepEqual(capture.personIdentity(`${BASE}?i=77`, html), { n: null, p: null, oc: null });
  const canonical = html.replace(
    "<head>",
    '<head><link rel="canonical" href="https://gw.geneanet.org/exampletree_w?n=quill&amp;p=sam&amp;oc=1">',
  );
  assert.equal(capture.personIdentity(`${BASE}?i=77`, canonical).oc, "1");
  assert.equal(capture.personName(html), "Sam.1 Quill");
});

test("filenameFor", () => {
  assert.equal(
    capture.filenameFor("exampletree", "person", "6"),
    "geneanet-drift/exampletree/pages/person-6.html",
  );
  assert.equal(
    capture.filenameFor("exampletree", "history", null),
    "geneanet-drift/exampletree/pages/history-editing.html",
  );
  assert.equal(
    capture.filenameFor("exampletree", "roster", null),
    "geneanet-drift/exampletree/pages/roster.html",
  );
  assert.throws(() => capture.filenameFor("../x", "roster", null));
  assert.throws(() => capture.filenameFor("exampletree", "person", "../6"));
});

test("extractLinks keeps person links and drops m= links", () => {
  const links = capture.extractLinks(read("person_rich"));
  assert.deepEqual(links, [
    { n: "ashby", p: "edmund", oc: "0" },
    { n: "brook", p: "ivy", oc: "0" },
    { n: "quill", p: "nell", oc: "0" },
    { n: "ashby", p: "wren", oc: "0" },
    { n: "ashby", p: "thea", oc: "0" },
  ]);
});

test("extractLinks handles oc, i-only and empty-surname roster links", () => {
  const links = capture.extractLinks(read("list_ind"));
  assert.equal(links.length, 6);
  assert.deepEqual(links[3], { n: "quill", p: "sam", oc: "1" });
  assert.deepEqual(links[4], { i: "330" });
  assert.deepEqual(links[5], { n: "", p: "baby", oc: "0" });
});

test("historyEntries reads links and times", () => {
  const entries = capture.historyEntries(read("history_editing"));
  assert.equal(entries.length, 3);
  assert.deepEqual(
    entries.map(({ n, p, oc }) => ({ n, p, oc })),
    [
      { n: "fox", p: "june", oc: "0" },
      { n: "marsh", p: "pip", oc: "0" },
      { n: "quill", p: "sam", oc: "1" },
    ],
  );
  assert.ok(entries[0].at > entries[1].at && entries[1].at > entries[2].at);
  assert.match(entries[0].at, /^2026-10-2[89]T/);
});

test("stripForCapture keeps the media script and drops the rest", () => {
  const html = read("person_rich").replace(
    "</body>",
    '<script>var tracker = 1;</script><input type="hidden" name="digest" value="abc"></body>',
  );
  const stripped = capture.stripForCapture(html);
  assert.ok(stripped.includes('"doc_images"'));
  assert.ok(!stripped.includes("tracker"));
  assert.ok(!stripped.includes("digest"));
  assert.ok(stripped.includes("Rowan Ashby"));
});

test("hashString is deterministic and sensitive", () => {
  assert.equal(capture.hashString("abc"), capture.hashString("abc"));
  assert.notEqual(capture.hashString("abc"), capture.hashString("abd"));
  assert.equal(capture.hashString(""), "811c9dc5");
  assert.equal(capture.hashString("a"), "e40c292c");
});

test("mediaFromJson normalises protocol-relative URLs", () => {
  const media = capture.mediaFromJson(read("person_rich"));
  assert.equal(media.length, 1);
  assert.equal(media[0].docId, 1001);
  assert.equal(media[0].title, "Harbor, 1975");
  assert.equal(media[0].date, null);
  assert.ok(media[0].original.startsWith("https://gw.example.test/"));
  assert.ok(media[0].original.includes("normal.jpg"));
  assert.ok(media[0].thumbnail.includes("medium.jpg"));
  assert.deepEqual(capture.mediaFromJson(read("person_sparse")), []);
});

test("planCapture skips an unchanged page and indexes a changed one", () => {
  const message = {
    kind: "person",
    key: "6",
    name: "Rowan Ashby",
    n: "ashby",
    p: "rowan",
    oc: "0",
    html: capture.stripForCapture(read("person_rich")),
  };
  const first = capture.planCapture({}, message, "exampletree", "2026-11-04T00:00:00.000Z");
  assert.equal(first.unchanged, false);
  assert.equal(first.file, "geneanet-drift/exampletree/pages/person-6.html");
  assert.equal(first.entry.links.length, 5);
  assert.equal(first.entry.hash, capture.hashString(message.html));
  const again = capture.planCapture({ [first.file]: first.entry }, message, "exampletree", "later");
  assert.equal(again.unchanged, true);
  const edited = { ...message, html: `${message.html}<!-- x -->` };
  assert.equal(capture.planCapture({ [first.file]: first.entry }, edited, "exampletree", "later").unchanged, false);
});

test("toVisit puts recent changes first, then uncaptured roster people", () => {
  const person = (key, n, p, oc, capturedAt) => ({
    kind: "person", key, name: null, n, p, oc, capturedAt, hash: "x", links: [],
  });
  const index = {
    captures: {
      a: person("6", "ashby", "rowan", "0", "2026-11-01T00:00:00.000Z"),
      b: person("77", "quill", "sam", "1", "2026-10-01T00:00:00.000Z"),
      c: person("10", "fox", "june", "0", "2026-11-03T00:00:00.000Z"),
    },
  };
  const roster = [
    { n: "ashby", p: "rowan", oc: "0" },
    { n: "quill", p: "sam", oc: "1" },
    { n: "marsh", p: "pip", oc: "0" },
    { n: "fox", p: "june", oc: "0" },
    { n: "brook", p: "ivy", oc: "0" },
    { i: "330" },
  ];
  const history = [
    { n: "fox", p: "june", oc: "0", at: "2026-10-28T20:00:00.000Z" },
    { n: "quill", p: "sam", oc: "1", at: "2026-10-28T10:00:00.000Z" },
    { n: "ashby", p: "rowan", oc: "0", at: "2026-09-01T10:00:00.000Z" },
    { n: "new", p: "kid", oc: "0", at: "2026-10-30T10:00:00.000Z" },
  ];
  const result = capture.toVisit(index, roster, history, "2026-10-01T00:00:00.000Z");
  assert.deepEqual(result, [
    { n: "quill", p: "sam", oc: "1" },
    { n: "new", p: "kid", oc: "0" },
    { n: "marsh", p: "pip", oc: "0" },
    { n: "brook", p: "ivy", oc: "0" },
    { i: "330" },
  ]);
});

test("toVisit matches an i-only roster link to a capture key", () => {
  const index = {
    captures: { a: { kind: "person", key: "330", n: null, p: null, oc: null, capturedAt: "t" } },
  };
  assert.deepEqual(capture.toVisit(index, [{ i: "330" }], [], ""), []);
});

test("normalizeTree accepts a slug, a _w suffix or a pasted address", () => {
  assert.equal(capture.normalizeTree("  exampletree_w "), "exampletree");
  assert.equal(capture.normalizeTree("https://gw.geneanet.org/exampletree_w?lang=en"), "exampletree");
  assert.equal(capture.normalizeTree("exampletree"), "exampletree");
  assert.equal(capture.isValidTree("example-tree_2"), true);
  for (const bad of ["", "-x", "a b", "a/b", "é"]) {
    assert.equal(capture.isValidTree(bad), false, bad);
  }
});

test("personUrl builds the person page address and omits oc 0", () => {
  const base = "https://gw.geneanet.org/exampletree_w?lang=en";
  assert.equal(capture.personUrl("exampletree", { n: "ashby", p: "rowan", oc: "0" }), `${base}&n=ashby&p=rowan`);
  assert.equal(capture.personUrl("exampletree", { n: "quill", p: "sam", oc: "1" }), `${base}&n=quill&p=sam&oc=1`);
  assert.equal(capture.personUrl("exampletree", { n: "d'a b", p: "x&y", oc: "" }), `${base}&n=d'a%20b&p=x%26y`);
  assert.equal(capture.personUrl("exampletree", { i: "330" }), `${base}&i=330`);
});

test("personLabel names a link without exposing the index", () => {
  assert.equal(capture.personLabel({ n: "ashby", p: "rowan", oc: "0" }), "rowan ashby");
  assert.equal(capture.personLabel({ n: "quill", p: "sam", oc: "2" }), "sam quill (2)");
  assert.equal(capture.personLabel({ n: "", p: "", oc: "0" }), "Unnamed person");
  assert.equal(capture.personLabel({ i: "330" }), "Unnamed person");
});

test("mediaFilename sanitises the title and takes the extension from the URL", () => {
  const item = { docId: 12, title: 'Wedding: "Ashby/Quill" <1950>', original: "https://gw.example.test/a/b/original.PNG?t=1" };
  assert.equal(
    capture.mediaFilename("exampletree", "6", item),
    "geneanet-drift/exampletree/media/person-6/12-Wedding-Ashby-Quill-1950.png",
  );
  const bare = { docId: "7", title: "../..", original: "//gw.example.test/a/original" };
  assert.equal(capture.mediaFilename("exampletree", "6", bare), "geneanet-drift/exampletree/media/person-6/7.jpg");
  const long = { docId: 1, title: "x".repeat(200), original: "https://h/a.jpg" };
  assert.ok(capture.mediaFilename("exampletree", "6", long).length < 120);
  assert.throws(() => capture.mediaFilename("exampletree", "../x", item));
  assert.throws(() => capture.mediaFilename("exampletree", "6", { ...item, docId: "/" }));
});

test("planCapture stores media on person captures only", () => {
  const html = capture.stripForCapture(read("person_rich"));
  const person = { kind: "person", key: "6", name: "x", n: "ashby", p: "rowan", oc: "0", html };
  const entry = capture.planCapture({}, person, "exampletree", "t").entry;
  assert.ok(entry.media.length > 0);
  assert.deepEqual(Object.keys(entry.media[0]).sort(), ["date", "docId", "original", "thumbnail", "title"]);
  const roster = { kind: "roster", key: null, name: null, n: null, p: null, oc: null, html: read("list_ind") };
  assert.equal("media" in capture.planCapture({}, roster, "exampletree", "t").entry, false);
});

test("toVisitFromCaptures and rosterProgress read the saved roster, history and people", () => {
  const person = { kind: "person", key: "6", n: "ashby", p: "rowan", oc: "0", capturedAt: "2026-11-01T00:00:00.000Z" };
  const captures = {
    p: person,
    r: { kind: "roster", links: [{ n: "ashby", p: "rowan", oc: "0" }, { n: "marsh", p: "pip", oc: "0" }, { i: "330" }] },
    h: { kind: "history", links: [{ n: "marsh", p: "pip", oc: "0", at: "2026-10-30T00:00:00.000Z" }] },
  };
  assert.deepEqual(capture.rosterProgress(captures), { saved: 1, total: 3 });
  assert.deepEqual(capture.toVisitFromCaptures(captures, "2026-10-01"), [
    { n: "marsh", p: "pip", oc: "0" },
    { i: "330" },
  ]);
  assert.equal(capture.rosterProgress({ p: person }), null);
});

test("pageState reports what the popup says about the active tab", () => {
  const base = "https://gw.geneanet.org/exampletree_w";
  const person = { kind: "person", key: "6", n: "ashby", p: "rowan", oc: "0", media: [{ docId: 1 }] };
  const file = "geneanet-drift/exampletree/pages/person-6.html";
  const captures = { [file]: person };
  const at = (url, status, tree = "exampletree", caps = captures) =>
    capture.pageState({ tree, url, status, captures: caps });
  const url = `${base}?lang=en&n=ashby&p=rowan`;
  assert.equal(at(url, null, "").state, "no-tree");
  assert.equal(at("https://gw.geneanet.org/othertree_w?n=a&p=b", null).state, "elsewhere");
  assert.equal(at(url, { state: "saved", kind: "person", file }).state, "saved");
  assert.equal(at(url, { state: "unchanged", kind: "person", file }).state, "unchanged");
  assert.equal(at(url, null).state, "known");
  assert.equal(at(url, null).entry, person);
  assert.equal(at(`${base}?n=quill&p=sam`, null).state, "not-person");
  assert.equal(at(base, null).state, "not-person");
  assert.equal(at(`${base}?lang=en&n=quill&p=sam`, { state: "error", kind: "person", message: "boom" }).state, "error");
  assert.equal(at(`${base}/history/editing`, null).state, "waiting");
  assert.equal(at(`${base}/history/editing`, { state: "empty", kind: "history" }).state, "empty");
  assert.equal(at(`${base}?m=LIST_IND&pg=1`, null).state, "unsaved");
});

test("savedAsExpected compares the final path, not the requested name", () => {
  const file = "geneanet-drift/exampletree/pages/person-6.html";
  assert.equal(capture.savedAsExpected(`/mnt/saved/${file}`, file), true);
  assert.equal(capture.savedAsExpected(`D:\\saved\\${file.replace(/\//g, "\\")}`, file), true);
  assert.equal(capture.savedAsExpected("/mnt/saved/download.html", file), false);
  assert.equal(capture.baseName("/mnt/saved/download.html"), "download.html");
});

test("historyEntries reads rows whose time sits on its own line and skips rows with no person link", () => {
  const entries = capture.historyEntries(read("history_editing"));
  assert.deepEqual(entries.map((entry) => entry.p), ["june", "pip", "sam"]);
});
