"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const VISIBLE = 25;

const el = (id) => document.getElementById(id);

function pageSentence(page, tree) {
  const sentences = {
    "no-tree": "No tree name is set.",
    elsewhere: `This is not a page of the tree "${tree}".`,
    saved: "This page was saved.",
    unchanged: "This page was already saved and has not changed.",
    known: "This page was saved earlier. Reload it to check for changes.",
    "not-person": "This is not a person page, so it is not saved.",
    unsaved: "This page has not been saved yet. Reload it to save it.",
    waiting: "Waiting for the history rows to appear.",
    empty: "No history rows appeared within 10 seconds, so nothing was saved.",
    error: `This page could not be saved: ${page.message}`,
  };
  return sentences[page.state];
}

function showPage(page, tree) {
  const status = el("page");
  status.replaceChildren(pageSentence(page, tree));
  status.dataset.kind = page.state === "error" || page.state === "empty" ? "error" : "info";
  if (page.state === "no-tree") {
    status.append(" ", optionsLink("Set it in options."));
  }
}

function optionsLink(text) {
  const link = document.createElement("a");
  link.href = "options.html";
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = text;
  return link;
}

function showSaved(captures) {
  const progress = capture.rosterProgress(captures);
  if (progress === null) {
    el("saved").textContent = "";
    el("saved-note").textContent =
      "Open the tree's list of all individuals once and it is saved, which gives the total number of people.";
    return;
  }
  el("saved").textContent = `${progress.saved} of ${progress.total} people saved`;
  el("saved-note").textContent =
    `${progress.total} is the number of people on the tree's list of all individuals, which you save by opening that list once.`;
}

function visitItem(tree, link) {
  const item = document.createElement("li");
  const anchor = document.createElement("a");
  anchor.href = capture.personUrl(tree, link);
  anchor.target = "_blank";
  anchor.rel = "noopener";
  anchor.textContent = capture.personLabel(link);
  item.append(anchor);
  return item;
}

function renderVisit(tree, people, expanded) {
  const shown = expanded ? people : people.slice(0, VISIBLE);
  el("visit-list").replaceChildren(...shown.map((link) => visitItem(tree, link)));
  const button = el("show-all");
  button.hidden = people.length <= VISIBLE;
  button.textContent = expanded ? `Show first ${VISIBLE}` : `Show all ${people.length}`;
  button.setAttribute("aria-expanded", String(expanded));
}

function showVisit(tree, captures, lastSync) {
  const hasRoster = capture.rosterProgress(captures) !== null;
  const people = capture.toVisitFromCaptures(captures, lastSync);
  el("visit-list").hidden = people.length === 0;
  if (!hasRoster) {
    el("visit-note").textContent = "This list appears after you save the tree's list of all individuals.";
    el("show-all").hidden = true;
    return;
  }
  if (people.length === 0) {
    el("visit-note").textContent = "Every person on the list of all individuals is saved.";
    el("show-all").hidden = true;
    return;
  }
  const since = lastSync ? ` People edited since ${lastSync} come first.` : "";
  el("visit-note").textContent =
    `${people.length} people on the list of all individuals are not saved yet.${since}`;
  let expanded = false;
  renderVisit(tree, people, expanded);
  el("show-all").onclick = () => {
    expanded = !expanded;
    renderVisit(tree, people, expanded);
  };
}

function outcome(state, error) {
  if (state === "complete") {
    return null;
  }
  return state === "interrupted" ? error || "interrupted" : undefined;
}

// Resolves null on success or the failure reason. The listener is registered before the
// current state is read, so a download that finishes in between is not missed.
function waitForDownload(id) {
  return new Promise((resolve) => {
    const settle = (result) => {
      if (result !== undefined) {
        api.downloads.onChanged.removeListener(listener);
        resolve(result);
      }
    };
    const listener = (delta) => {
      if (delta.id === id && delta.state) {
        settle(outcome(delta.state.current, delta.error && delta.error.current));
      }
    };
    api.downloads.onChanged.addListener(listener);
    api.downloads.search({ id }).then(([item]) => item && settle(outcome(item.state, item.error)));
  });
}

// Every download is queued before any is awaited, so closing the popup cannot cancel the rest.
function startOne(tree, key, item) {
  return api.downloads
    .download({
      url: item.original,
      filename: capture.mediaFilename(tree, key, item),
      conflictAction: "overwrite",
      saveAs: false,
    })
    .then(
      (id) => waitForDownload(id),
      (error) => error.message,
    );
}

async function saveMedia(tree, entry) {
  const button = el("save-media");
  const failures = el("media-failures");
  button.disabled = true;
  failures.replaceChildren();
  el("media-progress").textContent = `Saving ${entry.media.length} original${entry.media.length === 1 ? "" : "s"}.`;
  const reasons = await Promise.all(entry.media.map((item) => startOne(tree, entry.key, item)));
  let done = 0;
  reasons.forEach((reason, index) => {
    if (reason === null) {
      done += 1;
      return;
    }
    const line = document.createElement("li");
    line.textContent = `"${entry.media[index].title || "Untitled"}" could not be saved: ${reason}`;
    failures.append(line);
  });
  el("media-progress").textContent =
    `Saved ${done} of ${entry.media.length} originals to the geneanet-drift folder in your downloads.`;
  button.disabled = false;
}

function showMedia(tree, entry) {
  const media = entry ? entry.media || [] : [];
  el("media").hidden = media.length === 0;
  if (media.length === 0) {
    return;
  }
  const button = el("save-media");
  button.textContent = `Save ${media.length} original${media.length === 1 ? "" : "s"}`;
  button.onclick = () => saveMedia(tree, entry);
}

async function activeUrl() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  return tab && tab.url ? tab.url : "";
}

async function main() {
  const { tree = "", lastSync = "", status = null, captures = {} } = await api.storage.local.get([
    "tree",
    "lastSync",
    "status",
    "captures",
  ]);
  const page = capture.pageState({ tree, url: await activeUrl(), status, captures });
  showPage(page, tree);
  showSaved(captures);
  showVisit(tree, captures, lastSync);
  showMedia(tree, page.entry);
}

main();
