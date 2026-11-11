"use strict";

const TOKEN = document.querySelector('meta[name="review-token"]').content;
const GROUPS = [
  { state: "needs_match", title: "Needs a match", empty: "Every person has a match or is new." },
  { state: "new", title: "New in his tree", empty: "No one in his tree is missing from Gramps." },
  { state: "differs", title: "Differs", empty: "No matched person differs from Gramps." },
  { state: "unchanged", title: "Unchanged", empty: "No matched person is unchanged." },
];
const STATUS_LABELS = { accepted: "Accepted", rejected: "Rejected", edited: "Edited" };
const NOT_REVIEWED = "Not reviewed";

const PLACE_LIST = "place-titles";
const state = { data: null, places: null, placesFailed: false, query: "", hideDone: true, open: {}, editing: new Set(), errors: {}, drafts: {} };
const view = document.getElementById("view");

function h(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : value);
  }
  node.append(...kids.flat().filter((kid) => kid !== null && kid !== undefined && kid !== false));
  return node;
}

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "X-Review-Token": TOKEN, "Content-Type": "application/json" } : { "X-Review-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The request failed.");
  return payload;
}

function flash(message) {
  const node = document.getElementById("flash");
  node.textContent = message || "";
  node.hidden = !message;
}

function setNotice(parts) {
  const node = document.getElementById("notice");
  node.replaceChildren(...(parts || []));
  node.hidden = !parts;
}

/* Theme */

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
  document.querySelector('meta[name="color-scheme"]').content = choice === "system" ? "light dark" : choice;
  document.querySelector('meta[name="theme-color"]').content = getComputedStyle(root).getPropertyValue("--surface").trim();
}

function initTheme() {
  const select = document.getElementById("theme");
  let saved = "system";
  try {
    saved = localStorage.getItem("theme") || "system";
  } catch (error) {
    saved = "system";
  }
  select.value = saved;
  applyTheme(saved);
  select.addEventListener("change", () => {
    try {
      localStorage.setItem("theme", select.value);
    } catch (error) {
      flash("The theme applies now but could not be saved.");
    }
    applyTheme(select.value);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(select.value));
}

/* Text marking */

function normalWord(word) {
  return word.replace(/[,.;:]+$/, "").toLowerCase();
}

function markedLine(line, others) {
  const known = new Set(others.flatMap((other) => other.split(/\s+/)).map(normalWord));
  const parts = line.split(/(\s+)/).map((part) => {
    if (part === "" || /^\s+$/.test(part) || known.has(normalWord(part))) return part;
    return h("mark", { text: part });
  });
  return h("p", {}, ...parts);
}

function lines(values, others, emptyText) {
  if (values.length === 0) return [h("p", { class: "muted", text: emptyText })];
  return values.map((value) => (others.length ? markedLine(value, others) : h("p", { text: value })));
}

/* Skeletons */

function block(size) {
  return h("div", { class: "skeleton", "data-size": size });
}

function skeletonList() {
  return h("div", { "aria-hidden": "true" },
    block("title"),
    h("div", { class: "stats" }, ...[1, 2, 3, 4, 5].map(() => block("card"))),
    block("bar"),
    h("div", { class: "skeleton-stack" }, ...[1, 2, 3, 4].map(() => block("bar"))));
}

function skeletonPerson() {
  return h("div", { "aria-hidden": "true" },
    block("bar"), block("title"),
    h("div", { class: "skeleton-stack" }, block("row"), block("row")));
}

/* Decisions */

async function mutate(path, body, after = {}) {
  const focused = document.activeElement && document.activeElement.dataset.focus;
  const before = state.data;
  let saved = false;
  try {
    flash("");
    await api(path, body);
    state.data = await api("/api/changes");
    if (routePlaces()) state.places = (await api("/api/places")).places;
    saved = true;
    if (after.commit) after.commit();
  } catch (error) {
    flash(error.message);
  }
  setNotice(saved && after.notice ? after.notice(before, state.data) : null);
  render({ keepFocus: saved && after.focus ? after.focus() : focused });
}

function decide(row, status) {
  const current = row.decision && row.decision.status;
  state.editing.delete(row.id);
  return mutate("/api/decisions", { id: row.id, status: current === status ? "pending" : status });
}

function faultField(row, message) {
  return row.editable === "fact" && /^the place/.test(message) ? "place" : "value";
}

function saveEdit(row, value) {
  state.errors[row.id] = null;
  state.drafts[row.id] = value;
  return api("/api/decisions", { id: row.id, status: "edited", value })
    .then(() => api("/api/changes"))
    .then((data) => {
      state.data = data;
      state.editing.delete(row.id);
      delete state.drafts[row.id];
      render({ keepFocus: `${row.id}:edit` });
    })
    .catch((error) => {
      state.errors[row.id] = error.message;
      render({ keepFocus: `${row.id}:${faultField(row, error.message)}` });
    });
}

const NO_MATCH = "none";

function visibleNeedsMatch() {
  return state.data.people.filter((p) => p.state === "needs_match" && isShown(p));
}

function stillNeedMatch(count) {
  if (count === 0) return "No one still needs a match.";
  return count === 1 ? "1 person still needs a match." : `${count} people still need a match.`;
}

function matchNotice(person, change, before, after) {
  const left = after.summary.needs_match;
  const resolved = Math.max(0, before.summary.needs_match - left - 1);
  const candidate = person.candidates.find((c) => c.change === change);
  const done = change === NO_MATCH
    ? `Marked "${person.name}" as new.`
    : `Matched "${person.name}" to "${candidate.name}" (Gramps ID ${candidate.gramps_id}).`;
  const cascade = resolved ? ` ${plural(resolved, "more person no longer needs", "more people no longer need")} a match.` : "";
  return [`${done} ${stillNeedMatch(left)}${cascade} `, h("a", { href: `#/person/${person.key}`, text: "Review or undo" })];
}

function nextChoiceFocus(index) {
  const next = visibleNeedsMatch()[index];
  if (!next) return `group:${GROUPS[0].state}`;
  return next.candidates.length ? `${next.candidates[0].change}:link` : `${next.key}:none`;
}

function chooseLink(person, change) {
  const onList = routePerson() === null && !routePlaces();
  const index = visibleNeedsMatch().findIndex((p) => p.key === person.key);
  const after = onList && change !== null
    ? { notice: (before, now) => matchNotice(person, change, before, now), focus: () => nextChoiceFocus(index) }
    : {};
  return mutate("/api/link", { person: person.key, change }, after);
}

/* List view */

function statCard(count, caption) {
  return h("div", { class: "stat-card" }, h("dt", { text: caption }), h("dd", { class: "stat", text: String(count) }));
}

function summaryStats(summary) {
  return h("dl", { class: "stats" },
    statCard(summary.people, "People captured from his tree"),
    statCard(summary.needs_match, "People who need a match in Gramps"),
    statCard(summary.new, "People in his tree and not in Gramps"),
    statCard(summary.differs, "Matched people who differ from Gramps"),
    statCard(summary.unchanged, "Matched people with no differences"));
}

function changeCount(person) {
  return `${plural(person.actionable, "change", "changes")} to review, ${person.decided} decided`;
}

function personMeta(person) {
  const parts = [];
  if (person.years) parts.push(person.years);
  if (person.state === "needs_match") parts.push(plural(person.candidates.length, "possible match", "possible matches"));
  else if (person.state === "unchanged" && person.mine) parts.push(`Matched to "${person.mine.name}" (Gramps ID ${person.mine.id})`);
  else parts.push(changeCount(person));
  return parts.join(". ");
}

function personItem(person) {
  return h("li", {}, h("a", { class: "person-link", href: `#/person/${person.key}` },
    h("span", { text: person.name }), h("span", { class: "meta", text: personMeta(person) })),
  person.state === "needs_match" ? matchChoices(person) : null);
}

function matchesQuery(person) {
  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  return [person.name, person.mine && person.mine.name].some((name) => name && name.toLowerCase().includes(query));
}

function finished(person) {
  return (person.state === "new" || person.state === "differs") && person.actionable > 0 && person.decided >= person.actionable;
}

function leftToDecide(person) {
  if (person.state === "needs_match") return true;
  return (person.state === "new" || person.state === "differs") && !finished(person);
}

function isShown(person) {
  return matchesQuery(person) && !(state.hideDone && finished(person));
}

function groupSection(group, people) {
  const shown = people.filter(isShown);
  const count = shown.length !== people.length ? `${shown.length} of ${plural(people.length, "person", "people")}` : plural(people.length, "person", "people");
  const open = group.state in state.open ? state.open[group.state] : people.some(leftToDecide);
  const body = shown.length
    ? h("ul", { class: "people" }, ...shown.map(personItem))
    : h("p", { class: "muted", text: people.length ? "No one here matches the search or has changes left to decide." : group.empty });
  return h("details", { class: "group", open: open, ontoggle: (event) => { state.open[group.state] = event.target.open; } },
    h("summary", { "data-focus": `group:${group.state}`, text: `${group.title} (${count})` }), body);
}

function photoItem(person) {
  return h("li", {}, h("a", { class: "person-link", href: `#/person/${person.key}` },
    h("span", { text: person.name }),
    h("span", { class: "meta", text: `${plural(person.photos_missing, "photo", "photos")} not saved` })),
  h("p", { class: "links" }, openLink(person.his_url, "Open in Geneanet")));
}

function photosSection(people) {
  const waiting = people.filter((p) => p.photos_missing > 0);
  if (!waiting.length) return null;
  const shown = waiting.filter(matchesQuery);
  const count = shown.length !== waiting.length ? `${shown.length} of ${plural(waiting.length, "person", "people")}` : plural(waiting.length, "person", "people");
  const body = shown.length
    ? h("ul", { class: "people" }, ...shown.map(photoItem))
    : h("p", { class: "muted", text: "No one here matches the search." });
  return h("details", { class: "group", open: state.open.photos === true, ontoggle: (event) => { state.open.photos = event.target.open; } },
    h("summary", { "data-focus": "group:photos", text: `Photos to save (${count})` }),
    h("p", { class: "muted", text: "Open his page in Geneanet and use Save originals in the extension. Reload the review page afterwards." }), body);
}

function groupSections() {
  const { people } = state.data;
  const groups = GROUPS.map((group) => groupSection(group, people.filter((p) => p.state === group.state)));
  return [...groups, photosSection(people)].filter(Boolean);
}

function filterGroups() {
  document.getElementById("groups").replaceChildren(...groupSections());
}

function searchField() {
  let composing = false;
  const input = h("input", { id: "search", type: "search", value: state.query, autocomplete: "off",
    oninput: (event) => {
      state.query = event.target.value;
      if (!composing) filterGroups();
    },
    oncompositionstart: () => { composing = true; },
    oncompositionend: (event) => {
      composing = false;
      state.query = event.target.value;
      filterGroups();
    } });
  return h("div", { class: "field search" }, h("label", { for: "search", text: "Search people by name" }), input);
}

function hideDoneField() {
  return h("div", { class: "field field-check" },
    h("label", { for: "hide-done" },
      h("input", { id: "hide-done", type: "checkbox", checked: state.hideDone, onchange: (event) => {
        state.hideDone = event.target.checked;
        filterGroups();
      } }), "Hide people with no changes left to decide"));
}

function listView() {
  const { summary } = state.data;
  const empty = summary.people === 0
    ? h("p", { class: "status", role: "status", text: "No people were captured, so there is nothing to review." })
    : null;
  return h("div", {},
    h("h1", { tabindex: "-1", text: "Review changes" }),
    summaryStats(summary), empty,
    h("p", { class: "links" }, h("a", { class: "button", href: "#/places", text: "Places" })),
    searchField(), hideDoneField(),
    h("div", { id: "groups" }, ...groupSections()));
}

/* Person view */

function badge(decision) {
  return h("span", { class: "badge", text: decision ? STATUS_LABELS[decision.status] : NOT_REVIEWED });
}

function editFields(row) {
  const error = state.errors[row.id];
  const fault = error ? faultField(row, error) : null;
  const flag = (name) => ({ "aria-invalid": fault === name ? "true" : null, "aria-describedby": fault === name ? `${row.id}-error` : null });
  const saved = row.decision && row.decision.status === "edited" ? row.decision.value : row.edit_seed;
  const current = row.id in state.drafts ? state.drafts[row.id] : saved;
  const message = error ? h("p", { class: "error", id: `${row.id}-error`, text: error }) : null;
  if (row.editable === "fact") {
    return [
      h("div", { class: "field" }, h("label", { for: `${row.id}-date`, text: "Date, as you want it written" }),
        h("input", { id: `${row.id}-date`, name: "date_text", value: current.date_text, "data-focus": `${row.id}:value`, ...flag("value") })),
      h("div", { class: "field" }, h("label", { for: `${row.id}-place`, text: "Place" }),
        h("input", { id: `${row.id}-place`, name: "place", value: current.place, list: PLACE_LIST, autocomplete: "off", "data-focus": `${row.id}:place`, ...flag("place") })),
      message,
    ];
  }
  const control = row.editable === "note"
    ? h("textarea", { id: `${row.id}-value`, name: "value", "data-focus": `${row.id}:value`, ...flag("value") }, current)
    : h("input", { id: `${row.id}-value`, name: "value", value: current, "data-focus": `${row.id}:value`, ...flag("value") });
  return [h("div", { class: "field" }, h("label", { for: `${row.id}-value`, text: "Value for Gramps" }), control), message];
}

function editForm(row) {
  const form = h("form", {
    onsubmit: (event) => {
      event.preventDefault();
      const fields = Object.fromEntries(new FormData(form));
      saveEdit(row, row.editable === "fact" ? fields : fields.value);
    },
  }, ...editFields(row),
  h("div", { class: "actions" },
    h("button", { class: "button", type: "submit", "data-variant": "primary", text: "Save edit" }),
    h("button", { class: "button", type: "button", text: "Cancel",
      onclick: () => { state.editing.delete(row.id); state.errors[row.id] = null; delete state.drafts[row.id]; render({ keepFocus: `${row.id}:edit` }); } })));
  return form;
}

function rowActions(row) {
  const status = row.decision && row.decision.status;
  const button = (action, label, pressed, onclick) => h("button", {
    class: "button", type: "button", "data-action": action, "data-focus": `${row.id}:${action}`,
    "aria-pressed": pressed ? "true" : "false", text: label, onclick,
  });
  return h("div", { class: "actions" },
    button("accept", "Accept", status === "accepted", () => decide(row, "accepted")),
    row.editable ? button("edit", "Edit", status === "edited" || state.editing.has(row.id), () => {
      state.editing.add(row.id);
      render({ keepFocus: `${row.id}:value` });
    }) : null,
    button("reject", "Reject", status === "rejected", () => decide(row, "rejected")));
}

function waitingNote(row) {
  return h("p", { class: "muted" }, "Choose a match for \"",
    h("a", { href: `#/person/${row.waiting_on}`, text: row.waiting_name }), "\" first.");
}

function changeRow(row) {
  const both = row.his.length > 0 && row.mine.length > 0;
  const hisEmpty = row.informational ? "Not in his tree." : "Nothing recorded.";
  return h("article", { class: "row", "aria-label": row.label },
    h("div", { class: "row-head" }, h("h3", { tabindex: "-1", "data-focus": `${row.id}:heading`, text: row.label }), row.informational ? null : badge(row.decision)),
    row.sensitive ? h("p", { class: "status", role: "note",
      text: "Sensitive: this note may hold private contact details. Read it before accepting." }) : null,
    h("div", { class: "compare" },
      h("div", { class: "cell" }, h("p", { class: "cell-label", text: "His tree" }), ...lines(row.his, both ? row.mine : [], hisEmpty),
        row.his_place ? h("p", { class: "muted", text: `Gramps place: "${row.his_place}"` }) : null,
        row.photo ? h("img", { class: "thumb", src: row.photo, alt: row.his[0] || "Captured photo" }) : null,
        row.photo_missing ? h("p", { class: "muted", text: "Photo not saved. Open his page in Geneanet and use Save originals in the extension." }) : null),
      h("div", { class: "cell" }, h("p", { class: "cell-label", text: "Gramps" }), ...lines(row.mine, both ? row.his : [], "Not in Gramps."),
        row.mine_photos.length ? h("div", { class: "thumbs" }, ...row.mine_photos.map((p) => h("img", { class: "thumb", src: p.url, alt: p.alt }))) : null)),
    row.proposed ? h("p", { class: "muted", text: `Gramps would hold: ${row.proposed.join("; ")}` }) : null,
    row.edit_lines ? h("p", { text: `Your edit: ${row.edit_lines.join("; ")}` }) : null,
    row.waiting_on === null ? null : waitingNote(row),
    row.informational && row.waiting_on === null ? h("p", { class: "muted", text: "Shown for information. This is never changed in Gramps." }) : null,
    row.informational ? null : rowActions(row),
    state.editing.has(row.id) ? editForm(row) : null);
}

function openLink(url, text) {
  if (!url) return null;
  return h("a", { class: "button", href: url, target: "_blank", rel: "noopener noreferrer", text });
}

function candidateItem(person, candidate) {
  const chosen = candidate.decision && candidate.decision.status === "accepted";
  const label = [candidate.name, `(Gramps ID ${candidate.gramps_id})`, candidate.years].filter(Boolean).join(" ");
  const text = chosen ? "Chosen. Undo" : "Use this person";
  return h("li", {}, h("span", { text: label }),
    h("span", { class: "actions" }, openLink(candidate.mine_url, "Open in Gramps"),
      h("button", { class: "button", type: "button", "aria-pressed": chosen ? "true" : "false",
        "aria-label": `${text}: ${candidate.name} (Gramps ID ${candidate.gramps_id}) for "${person.name}"`,
        "data-focus": `${candidate.change}:link`, text,
        onclick: () => chooseLink(person, chosen ? null : candidate.change) })));
}

function noneButton(person) {
  return h("button", { class: "button", type: "button", "data-focus": `${person.key}:none`,
    "aria-label": `None of these is the same person as "${person.name}"`,
    text: "None of these is the same person", onclick: () => chooseLink(person, NO_MATCH) });
}

function matchChoices(person) {
  return h("div", { class: "choices" },
    h("ul", { class: "candidates" }, ...person.candidates.map((c) => candidateItem(person, c))),
    person.state === "needs_match" ? noneButton(person) : null);
}

function refusedSection(person) {
  return h("section", { "aria-labelledby": "match-title" },
    h("h2", { id: "match-title", text: "Matching person in Gramps" }),
    h("p", { class: "muted", text: "You said none of the Gramps people is this person." }),
    h("button", { class: "button", type: "button", "data-focus": `${person.key}:none`, text: "Undo",
      onclick: () => chooseLink(person, null) }));
}

function matchSection(person) {
  if (person.refused) return refusedSection(person);
  if (person.candidates.length === 0) return null;
  const note = person.state === "needs_match"
    ? "Names match but nothing confirms it. Choose the one that is the same person."
    : "You chose this person. Undo the choice to pick another.";
  return h("section", { "aria-labelledby": "match-title" },
    h("h2", { id: "match-title", text: "Matching person in Gramps" }),
    h("p", { class: "muted", text: note }),
    matchChoices(person));
}

function personSummary(person) {
  const base = [person.years, person.mine ? `Matched to "${person.mine.name}" (Gramps ID ${person.mine.id}): ${person.why}` : null];
  return h("p", { class: "muted", text: base.filter(Boolean).join(". ") || "No dates recorded." });
}

function placeDatalist() {
  return h("datalist", { id: PLACE_LIST }, ...state.data.place_titles.map((place) => h("option", { value: place.title })));
}

function neighbours(person) {
  const sequence = GROUPS.filter((g) => g.state !== "unchanged").flatMap((group) =>
    state.data.people.filter((p) => p.state === group.state && (p === person || isShown(p))));
  const index = sequence.indexOf(person);
  if (index < 0) return null;
  const group = GROUPS.find((g) => g.state === person.state);
  const inGroup = sequence.filter((p) => p.state === person.state);
  return { previous: sequence[index - 1], next: sequence[index + 1], caption: `${group.title}: ${inGroup.indexOf(person) + 1} of ${inGroup.length}` };
}

function neighbourLink(label, person, slot) {
  if (!person) return h("span", { class: "nav-slot" });
  return h("a", { class: "nav-slot", "data-slot": slot, href: `#/person/${person.key}`, text: `${label}: "${person.name}"` });
}

function personNav(person, label) {
  const around = neighbours(person);
  if (!around) return null;
  return h("nav", { class: "person-nav", "aria-label": label },
    h("p", { class: "muted", text: around.caption }),
    h("div", { class: "nav-links" }, neighbourLink("Previous", around.previous, "previous"), neighbourLink("Next", around.next, "next")));
}

function personView(person) {
  const rows = person.changes;
  const body = rows.length
    ? rows.map(changeRow)
    : [h("p", { class: "status", role: "status", text: person.state === "needs_match" ? "Choose a match to see the changes." : "No differences from Gramps." })];
  return h("div", {},
    placeDatalist(),
    h("a", { class: "back", href: "#/", text: "Back to all people" }),
    personNav(person, "Other people"),
    h("h1", { tabindex: "-1", text: person.name }),
    personSummary(person),
    h("p", { class: "links" }, openLink(person.his_url, "Open in Geneanet"), person.mine && openLink(person.mine.url, "Open in Gramps")),
    matchSection(person),
    h("h2", { text: changeCount(person) }), ...body,
    personNav(person, "Other people, end of page"));
}

function missingPersonView() {
  return h("div", {},
    h("a", { class: "back", href: "#/", text: "Back to all people" }),
    h("h1", { tabindex: "-1", text: "Person not found" }),
    h("p", { class: "status", role: "status", text: "No person has this number." }));
}

/* Places view */

function placeStatus(place) {
  if (!place.mapping) return h("p", { text: `Will create a new place named "${place.text}"` });
  return h("p", {}, `Gramps place: "${place.mapping.title}"`);
}

function placeButton(place, index, action, label, handle) {
  return h("button", { class: "button", type: "button", "data-focus": `place-${index}:${action}`, text: label,
    onclick: () => mutate("/api/places", { text: place.text, handle }) });
}

function placeForm(place, index) {
  const key = `place-${index}`;
  const error = state.errors[key];
  const form = h("form", {
    onsubmit: (event) => {
      event.preventDefault();
      const title = new FormData(form).get("title");
      const found = state.data.place_titles.find((p) => p.title === title);
      state.drafts[key] = title;
      state.errors[key] = found ? null : "Choose one of the Gramps place titles in the list.";
      if (found) mutate("/api/places", { text: place.text, handle: found.handle }, { commit: () => { delete state.drafts[key]; } });
      else render({ keepFocus: `${key}:title` });
    },
  },
  h("div", { class: "field" }, h("label", { for: `${key}-title`, text: "Another Gramps place" }),
    h("input", { id: `${key}-title`, name: "title", value: state.drafts[key] || "", list: PLACE_LIST, autocomplete: "off", "data-focus": `${key}:title`,
      "aria-invalid": error ? "true" : null, "aria-describedby": error ? `${key}-error` : null }),
    error ? h("p", { class: "error", id: `${key}-error`, text: error }) : null),
  h("div", { class: "actions" }, h("button", { class: "button", type: "submit", text: "Save" })));
  return form;
}

function placeRow(place, index) {
  const chosen = place.mapping && place.mapping.handle;
  const suggestions = place.suggestions.filter((s) => s.handle !== chosen);
  return h("article", { class: "row", "aria-label": place.text },
    h("div", { class: "row-head" }, h("h3", { tabindex: "-1", "data-focus": `place-${index}:heading`, text: `"${place.text}"` }),
      h("span", { class: "muted", text: `Used ${plural(place.uses, "time", "times")}` })),
    placeStatus(place),
    h("div", { class: "actions" },
      place.mapping ? placeButton(place, index, "clear", "Clear", null) : null,
      ...suggestions.map((s) => placeButton(place, index, `use-${s.handle}`, `Use "${s.title}"`, s.handle))),
    placeForm(place, index));
}

function placesView() {
  return h("div", {},
    placeDatalist(),
    h("a", { class: "back", href: "#/", text: "Back to all people" }),
    h("h1", { tabindex: "-1", text: "Places" }),
    h("p", { class: "muted", text: "Each place text in his tree and the Gramps place it becomes. A text with no choice creates a new place when applied." }),
    state.places.length
      ? state.places.map(placeRow)
      : h("p", { class: "status", role: "status", text: "No place texts were captured." }));
}

/* Routing and rendering */

function routePlaces() {
  return location.hash === "#/places";
}

async function loadPlaces() {
  try {
    state.places = (await api("/api/places")).places;
    state.placesFailed = false;
  } catch (error) {
    state.placesFailed = true;
    flash(error.message);
  }
  render({ moveFocus: true });
}

function routePerson() {
  const found = location.hash.match(/^#\/person\/(\d+)$/);
  return found ? Number(found[1]) : null;
}

function updateApply() {
  const count = state.data ? state.data.summary.accepted : 0;
  document.getElementById("apply").textContent = `Accepted changes (${count})`;
}

function focusTarget(key) {
  return [...view.querySelectorAll("[data-focus]")].find((node) => node.dataset.focus === key);
}

function focusOrFallback(key) {
  const owner = key.slice(0, key.lastIndexOf(":"));
  const target = focusTarget(key) || focusTarget(`${owner}:heading`) || view.querySelector("h1");
  if (target) target.focus();
}

function render({ keepFocus = null, moveFocus = false } = {}) {
  if (!state.data) {
    view.replaceChildren(routePerson() === null ? skeletonList() : skeletonPerson());
    view.setAttribute("aria-busy", "true");
    return;
  }
  if (routePlaces() && state.places === null && state.placesFailed) {
    view.replaceChildren(h("div", {},
      h("a", { class: "back", href: "#/", text: "Back to all people" }),
      h("h1", { tabindex: "-1", text: "Places" }),
      h("p", { class: "status", role: "status", text: "The places could not be loaded." }),
      h("button", { class: "button", type: "button", text: "Try again", onclick: loadPlaces })));
    view.removeAttribute("aria-busy");
    return;
  }
  if (routePlaces() && state.places === null) {
    view.replaceChildren(skeletonPerson());
    view.setAttribute("aria-busy", "true");
    return;
  }
  view.removeAttribute("aria-busy");
  const key = routePerson();
  const person = key === null ? null : state.data.people.find((p) => p.key === key);
  if (key !== null && !person && !routePlaces()) {
    view.replaceChildren(missingPersonView());
    document.title = "No such person - Review changes";
  } else {
    view.replaceChildren(routePlaces() ? placesView() : person ? personView(person) : listView());
    document.title = routePlaces() ? "Places - Review changes" : person ? `${person.name} - Review changes` : "Review changes";
  }
  updateApply();
  if (moveFocus) view.querySelector("h1").focus();
  if (keepFocus) focusOrFallback(keepFocus);
}

/* Plan dialog */

function planItem(item) {
  const text = `${item.action}: ${item.label} for "${item.person}"${item.edited ? " (edited)" : ""}`;
  return h("li", {}, text, item.sensitive ? h("span", { class: "muted", text: " Sensitive note." }) : null,
    item.lines.length ? h("div", { class: "muted", text: item.lines.join("; ") }) : null,
    item.reason ? h("div", { class: "muted", text: `Not written: ${item.reason}.` }) : null);
}

function planBody(plan) {
  if (!plan.count) {
    return [h("p", { class: "status", role: "status", text: "No changes are accepted yet. Accept some changes first." })];
  }
  return [
    plan.items.length ? h("ol", { class: "plan" }, ...plan.items.map(planItem)) : null,
    plan.skipped.length ? h("h3", { text: "Not written" }) : null,
    plan.skipped.length ? h("ol", { class: "plan" }, ...plan.skipped.map(planItem)) : null,
    h("p", { text: "Nothing is written from this page. Run this in a terminal; it saves a backup of Gramps, lists the exact steps and asks before writing:" }),
    h("p", {}, h("code", { text: plan.command })),
  ];
}

async function openPlan() {
  const dialog = document.getElementById("plan");
  const body = document.getElementById("plan-body");
  try {
    const plan = await api("/api/preview");
    document.getElementById("plan-title").textContent = `Accepted changes (${plan.count})`;
    body.replaceChildren(...planBody(plan));
  } catch (error) {
    body.replaceChildren(h("p", { class: "status", "data-kind": "error", role: "alert", text: error.message }));
  }
  dialog.showModal();
}

async function start() {
  initTheme();
  document.getElementById("apply").addEventListener("click", openPlan);
  document.getElementById("plan-close").addEventListener("click", () => document.getElementById("plan").close());
  window.addEventListener("hashchange", () => {
    setNotice(null);
    if (routePlaces()) loadPlaces();
    else render({ moveFocus: true });
  });
  render();
  try {
    state.data = await api("/api/changes");
    if (routePlaces()) await loadPlaces();
    else render();
  } catch (error) {
    view.replaceChildren(h("p", { class: "status", "data-kind": "error", role: "alert", text: `The review could not load: ${error.message}` }));
  }
}

start();
