// storage.local layout:
//   tree      tree slug, set on the options page
//   lastSync  YYYY-MM-DD of the last review, set on the options page
//   status    {at, state: "saved" | "unchanged" | "empty" | "error", kind, file, message}
//   captures  {<file>: {kind, key, name, n, p, oc, capturedAt, hash, links}}
//             <file> is the path under Downloads. key is the GeneWeb index `i` for people.
//             links are the outgoing person links {n, p, oc} (or {i} when a link has no name).
//             history captures hold {n, p, oc, at} per row instead, `at` an ISO UTC time.
const capture = (() => {
  "use strict";

  const ROOT = "geneanet-drift";
  const SLUG = /^[A-Za-z0-9_-]+$/;
  const MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];

  function decode(text) {
    return text.replace(/&amp;/g, "&");
  }

  function safeDecode(text) {
    try {
      return decodeURIComponent(text);
    } catch {
      return text;
    }
  }

  function parseQuery(source) {
    const query = String(source).split("#")[0];
    const text = query.includes("?") ? query.slice(query.indexOf("?") + 1) : query;
    const found = {};
    for (const pair of decode(text).split("&")) {
      if (pair !== "") {
        const [name, ...rest] = pair.split("=");
        found[safeDecode(name)] = safeDecode(rest.join("=").replace(/\+/g, " "));
      }
    }
    return found;
  }

  function pathOf(url) {
    const clean = String(url).split(/[?#]/)[0];
    return clean.replace(/^https?:\/\/[^/]+/, "");
  }

  function treeOf(url) {
    const match = pathOf(url).match(/^\/([^/]+)_w(?:\/|$)/);
    return match ? match[1] : null;
  }

  function hasEntries(html) {
    return html.includes("<gw-history-editing-entry");
  }

  function hasDigest(html) {
    return /<input\b[^>]*\bname=["']?digest["']?/i.test(html);
  }

  function headingOf(html) {
    const match = html.match(/<h1\b[^>]*\bclass="[^"]*\bname\b[^"]*"[^>]*>([\s\S]*?)<\/h1>/);
    return match ? match[1] : null;
  }

  function personKeyFromHtml(html) {
    const heading = headingOf(html);
    const match = heading && decode(heading).match(/[?&]m=MOD_IND&i=(\d+)/);
    return match ? match[1] : null;
  }

  function personName(html) {
    const heading = headingOf(html);
    if (heading === null) {
      return null;
    }
    const text = heading.replace(/<[^>]*>/g, "").replace(/&nbsp;|\s/g, " ");
    return text.replace(/\s+/g, " ").trim() || null;
  }

  function classify(url, html) {
    const query = parseQuery(String(url).includes("?") ? url : "");
    const path = pathOf(url);
    if (hasDigest(html) || query.m === "MOD_IND" || query.m === "MOD_FAM") {
      return null;
    }
    if (/_w\/history\/editing\/?$/.test(path) || (/_w\/history\/?$/.test(path) && hasEntries(html))) {
      return { kind: "history", key: null };
    }
    if (query.m === "LIST_IND") {
      return { kind: "roster", key: null };
    }
    const key = personKeyFromHtml(html);
    return key === null ? null : { kind: "person", key };
  }

  function canonicalHref(html) {
    const match = html.match(/<link\b[^>]*\brel="canonical"[^>]*\bhref="([^"]*)"/);
    return match ? decode(match[1]) : null;
  }

  function personIdentity(url, html) {
    const canonical = canonicalHref(html);
    const source = canonical !== null && /[?&]n=/.test(canonical) ? canonical : url;
    const query = parseQuery(String(source).includes("?") ? source : "");
    return { n: query.n ?? null, p: query.p ?? null, oc: query.oc ?? null };
  }

  function filenameFor(tree, kind, key) {
    if (!SLUG.test(tree)) {
      throw new Error(`Invalid tree slug: ${tree}`);
    }
    const names = { history: "history-editing.html", roster: "roster.html" };
    if (kind === "person") {
      if (!/^\d+$/.test(String(key))) {
        throw new Error(`Invalid person key: ${key}`);
      }
      return `${ROOT}/${tree}/pages/person-${key}.html`;
    }
    if (!names[kind]) {
      throw new Error(`Unknown page kind: ${kind}`);
    }
    return `${ROOT}/${tree}/pages/${names[kind]}`;
  }

  function linkOf(href) {
    const [path] = decode(href).split(/[?#]/);
    if (!/(?:^|\/)[^/]+_w$/.test(path)) {
      return null;
    }
    const query = parseQuery(href);
    if (query.m !== undefined) {
      return null;
    }
    if (query.n === undefined && query.p === undefined) {
      return query.i === undefined ? null : { i: query.i };
    }
    return { n: query.n ?? "", p: query.p ?? "", oc: query.oc ?? "0" };
  }

  function identityOf(link) {
    return link.i !== undefined ? `i:${link.i}` : `${link.n}|${link.p}|${link.oc || "0"}`;
  }

  function extractLinks(html) {
    const seen = new Map();
    for (const match of html.matchAll(/\shref="([^"]*)"/g)) {
      const link = linkOf(match[1]);
      if (link !== null && !seen.has(identityOf(link))) {
        seen.set(identityOf(link), link);
      }
    }
    return [...seen.values()];
  }

  function historyTime(label, clock) {
    const date = label.match(/^(\w+) (\d+), (\d{4})$/);
    const time = clock.match(/^(\d+):(\d+):(\d+)$/);
    const month = date ? MONTHS.indexOf(date[1]) : -1;
    if (month < 0 || !time) {
      return null;
    }
    return new Date(
      Number(date[3]), month, Number(date[2]), Number(time[1]), Number(time[2]), Number(time[3]),
    ).toISOString();
  }

  function historyEntries(html) {
    const rows = html.split("<gw-history-editing-entry").slice(1);
    const entries = [];
    for (const row of rows) {
      const stamp = row.match(/<b[^>]*>([^<]*)<\/b>\s*<div[^>]*>([^<]*)<\/div>/);
      const link = linkOf((row.match(/\shref="([^"]*)"/) || [])[1] || "");
      const at = stamp ? historyTime(stamp[1].trim(), stamp[2].trim()) : null;
      if (link !== null && at !== null) {
        entries.push({ ...link, at });
      }
    }
    return entries;
  }

  function stripForCapture(html) {
    return html
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, (script) =>
        script.includes('"doc_images"') ? script : "",
      )
      .replace(/<input\b[^>]*\bname=["']?digest["']?[^>]*>/gi, "");
  }

  // FNV-1a over UTF-16 code units: change detection only, not integrity.
  function hashString(text) {
    let hash = 0x811c9dc5;
    for (let index = 0; index < text.length; index++) {
      hash = Math.imul(hash ^ text.charCodeAt(index), 0x01000193) >>> 0;
    }
    return hash.toString(16).padStart(8, "0");
  }

  // End offset of the JSON object starting at `start`, or -1; braces inside strings do not count.
  function objectEnd(text, start) {
    let depth = 0;
    let inString = false;
    for (let index = start; index < text.length; index++) {
      const char = text[index];
      if (inString) {
        if (char === "\\") {
          index++;
        } else if (char === '"') {
          inString = false;
        }
      } else if (char === '"') {
        inString = true;
      } else if (char === "{") {
        depth++;
      } else if (char === "}" && --depth === 0) {
        return index + 1;
      }
    }
    return -1;
  }

  // The media list sits inside a larger page configuration object, so pick out the items themselves.
  function mediaObjects(script) {
    const items = [];
    let at = script.indexOf("{");
    while (at !== -1) {
      const end = objectEnd(script, at);
      let item = null;
      if (end !== -1) {
        try {
          item = JSON.parse(script.slice(at, end));
        } catch {
          item = null;
        }
      }
      if (item && item.doc_images) {
        items.push(item);
        at = script.indexOf("{", end);
      } else {
        at = script.indexOf("{", at + 1);
      }
    }
    return items;
  }

  function mediaFromJson(html) {
    const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
      .map((match) => match[1])
      .find((body) => body.includes('"doc_images"'));
    if (script === undefined) {
      return [];
    }
    const https = (url) => (String(url).startsWith("//") ? `https:${url}` : url);
    const seen = new Set();
    return mediaObjects(script)
      .map((item) => ({
        docId: item.doc_id,
        title: item.doc_title,
        date: item.doc_date,
        original: https(item.doc_images.original),
        thumbnail: https(item.doc_images.thumbnail),
      }))
      .filter((item) => !seen.has(item.original) && seen.add(item.original));
  }

  function safeMedia(html) {
    try {
      return mediaFromJson(html);
    } catch {
      return [];
    }
  }

  // Builds the index entry for a content-script message, or reports the page unchanged.
  function planCapture(captures, message, tree, nowIso) {
    const file = filenameFor(tree, message.kind, message.key);
    const hash = hashString(message.html);
    if (captures[file] && captures[file].hash === hash) {
      return { file, unchanged: true };
    }
    const links = message.kind === "history" ? historyEntries(message.html) : extractLinks(message.html);
    const entry = {
      kind: message.kind,
      key: message.key,
      name: message.name,
      n: message.n,
      p: message.p,
      oc: message.oc,
      capturedAt: nowIso,
      hash,
      links,
    };
    if (message.kind === "person") {
      entry.media = safeMedia(message.html);
    }
    return { file, unchanged: false, entry };
  }

  function capturedIdentities(captures) {
    const found = new Map();
    for (const entry of Object.values(captures)) {
      if (entry.kind === "person") {
        const when = entry.capturedAt;
        found.set(`i:${entry.key}`, when);
        if (entry.n !== null || entry.p !== null) {
          found.set(identityOf({ n: entry.n ?? "", p: entry.p ?? "", oc: entry.oc ?? "0" }), when);
        }
      }
    }
    return found;
  }

  // People changed after sinceIso and not recaptured since come first, then the rest of the
  // roster not yet captured.
  function toVisit(index, roster, history, sinceIso) {
    const captured = capturedIdentities(index.captures || {});
    const listed = new Set();
    const result = [];
    const add = (link) => {
      const id = identityOf(link);
      if (!listed.has(id)) {
        listed.add(id);
        result.push(link);
      }
    };
    // The options page stores a date; edits made on that day count as already reviewed.
    const since = /^\d{4}-\d{2}-\d{2}$/.test(sinceIso) ? `${sinceIso}T23:59:59.999Z` : sinceIso;
    for (const entry of history) {
      const when = captured.get(identityOf(entry));
      if (entry.at > since && (when === undefined || when < entry.at)) {
        add({ n: entry.n, p: entry.p, oc: entry.oc });
      }
    }
    for (const link of roster) {
      if (!captured.has(identityOf(link))) {
        add(link);
      }
    }
    return result;
  }

  function normalizeTree(text) {
    const trimmed = String(text).trim();
    const slug = /^https?:\/\//i.test(trimmed) ? treeOf(trimmed) || "" : trimmed;
    return slug.replace(/_w$/i, "");
  }

  function isValidTree(slug) {
    return /^[a-z0-9][a-z0-9_-]*$/i.test(slug);
  }

  function personUrl(tree, link) {
    const base = `https://gw.geneanet.org/${tree}_w?lang=en`;
    if (link.i !== undefined) {
      return `${base}&i=${encodeURIComponent(link.i)}`;
    }
    const oc = link.oc && link.oc !== "0" ? `&oc=${encodeURIComponent(link.oc)}` : "";
    return `${base}&n=${encodeURIComponent(link.n)}&p=${encodeURIComponent(link.p)}${oc}`;
  }

  function personLabel(link) {
    if (link.i !== undefined) {
      return "Unnamed person";
    }
    const name = [link.p, link.n].filter((part) => part !== "").join(" ") || "Unnamed person";
    return link.oc && link.oc !== "0" ? `${name} (${link.oc})` : name;
  }

  function capturesOfKind(captures, kind) {
    return Object.values(captures).filter((entry) => entry.kind === kind);
  }

  function toVisitFromCaptures(captures, sinceIso) {
    const roster = capturesOfKind(captures, "roster").flatMap((entry) => entry.links);
    const history = capturesOfKind(captures, "history").flatMap((entry) => entry.links);
    return toVisit({ captures }, roster, history, sinceIso);
  }

  // Null until a roster page has been saved.
  function rosterProgress(captures) {
    const rosters = capturesOfKind(captures, "roster");
    if (rosters.length === 0) {
      return null;
    }
    const roster = rosters.flatMap((entry) => entry.links);
    const saved = capturedIdentities(captures);
    return { saved: roster.filter((link) => saved.has(identityOf(link))).length, total: roster.length };
  }

  function findPersonEntry(captures, url) {
    const query = parseQuery(String(url).includes("?") ? url : "");
    const wanted = identityOf(
      query.n === undefined && query.p === undefined && query.i !== undefined
        ? { i: query.i }
        : { n: query.n ?? "", p: query.p ?? "", oc: query.oc ?? "0" },
    );
    for (const [file, entry] of Object.entries(captures)) {
      if (entry.kind !== "person") {
        continue;
      }
      const own = [`i:${entry.key}`, identityOf({ n: entry.n ?? "", p: entry.p ?? "", oc: entry.oc ?? "0" })];
      if (own.includes(wanted)) {
        return { file, entry };
      }
    }
    return null;
  }

  function pageKindOf(url) {
    const path = pathOf(url);
    if (/_w\/history(\/editing)?\/?$/.test(path)) {
      return "history";
    }
    const query = parseQuery(String(url).includes("?") ? url : "");
    if (query.m === "LIST_IND") {
      return "roster";
    }
    const named = query.n !== undefined || query.p !== undefined || query.i !== undefined;
    return named && query.m === undefined ? "person" : "other";
  }

  // What the popup says about the active tab. `file` and `entry` are set when this page has a
  // capture.
  function pageState({ tree, url, status, captures }) {
    if (!tree) {
      return { state: "no-tree" };
    }
    if (treeOf(url) !== tree) {
      return { state: "elsewhere" };
    }
    const kind = pageKindOf(url);
    const found = kind === "person" ? findPersonEntry(captures, url) : null;
    const file = found ? found.file : kind === "history" || kind === "roster" ? filenameFor(tree, kind) : null;
    const entry = found ? found.entry : null;
    const result = { kind, file, entry };
    if (status && file !== null && status.file === file && ["saved", "unchanged"].includes(status.state)) {
      return { ...result, state: status.state };
    }
    if (file !== null && captures[file]) {
      return { ...result, state: "known" };
    }
    if (status && status.kind === kind && status.state === "error") {
      return { ...result, state: "error", message: status.message };
    }
    if (kind === "history") {
      return { ...result, state: status && status.kind === "history" && status.state === "empty" ? "empty" : "waiting" };
    }
    return { ...result, state: kind === "roster" ? "unsaved" : "not-person" };
  }

  function extensionOf(url) {
    const match = pathOf(String(url).replace(/^\/\//, "https://x/")).match(/\.([A-Za-z0-9]{2,5})$/);
    return match ? match[1].toLowerCase() : "jpg";
  }

  function mediaFilename(tree, key, item) {
    if (!SLUG.test(tree) || !/^\d+$/.test(String(key))) {
      throw new Error(`Invalid media location: ${tree}/${key}`);
    }
    const title = String(item.title ?? "")
      .replace(/[^\p{L}\p{N}]+/gu, " ")
      .trim()
      .slice(0, 60)
      .trim()
      .replace(/ /g, "-");
    const id = String(item.docId).replace(/[^A-Za-z0-9]+/g, "");
    if (id === "") {
      throw new Error("Media item has no id");
    }
    const stem = title === "" ? id : `${id}-${title}`;
    return `${ROOT}/${tree}/media/person-${key}/${stem}.${extensionOf(item.original)}`;
  }

  // Another extension can rename a download after it was requested, so check the final path.
  function savedAsExpected(actualPath, file) {
    return String(actualPath).replace(/\\/g, "/").endsWith(`/${file}`);
  }

  function baseName(path) {
    return String(path).split(/[\\/]/).pop();
  }

  return {
    savedAsExpected,
    baseName,
    normalizeTree,
    isValidTree,
    personUrl,
    personLabel,
    toVisitFromCaptures,
    rosterProgress,
    findPersonEntry,
    pageState,
    mediaFilename,
    parseQuery,
    treeOf,
    classify,
    personKeyFromHtml,
    personName,
    personIdentity,
    filenameFor,
    extractLinks,
    historyEntries,
    stripForCapture,
    hashString,
    mediaFromJson,
    planCapture,
    toVisit,
  };
})();

if (typeof module !== "undefined") {
  module.exports = capture;
}
