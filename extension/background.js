"use strict";

if (typeof importScripts === "function") {
  importScripts("lib/capture.js");
}

const api = globalThis.browser ?? globalThis.chrome;
const DATA_URL = "data:text/html;charset=utf-8,";

let queue = Promise.resolve();

async function setStatus(state, fields) {
  await api.storage.local.set({ status: { at: new Date().toISOString(), state, ...fields } });
}

function finished(id) {
  return new Promise((resolve) => {
    const settle = ([item]) => {
      if (item && item.state !== "in_progress") {
        api.downloads.onChanged.removeListener(listener);
        resolve(item);
      }
    };
    const listener = (change) => {
      if (change.id === id && change.state) {
        api.downloads.search({ id }).then(settle);
      }
    };
    api.downloads.onChanged.addListener(listener);
    api.downloads.search({ id }).then(settle);
  });
}

async function verifySaved(id, file) {
  const item = await finished(id);
  if (item.state !== "complete") {
    throw new Error(`The browser could not save this page (${item.error || "interrupted"}).`);
  }
  if (!capture.savedAsExpected(item.filename, file)) {
    throw new Error(
      `The browser saved this page as "${capture.baseName(item.filename)}" instead of "${file}". ` +
        "Another extension that handles downloads may be renaming it. Turn it off and reload this page.",
    );
  }
}

async function save(message) {
  const { captures = {} } = await api.storage.local.get("captures");
  const plan = capture.planCapture(captures, message, message.tree, new Date().toISOString());
  if (plan.unchanged) {
    await setStatus("unchanged", { kind: message.kind, file: plan.file });
    return;
  }
  if (message.kind === "history" && plan.entry.links.length === 0) {
    throw new Error("The history rows could not be read. Set the Geneanet language to English and reload the page.");
  }
  const id = await api.downloads.download({
    url: DATA_URL + encodeURIComponent(message.html),
    filename: plan.file,
    conflictAction: "overwrite",
    saveAs: false,
  });
  await verifySaved(id, plan.file);
  captures[plan.file] = plan.entry;
  await api.storage.local.set({ captures });
  await setStatus("saved", { kind: message.kind, file: plan.file });
}

async function handle(message) {
  try {
    if (message.type === "capture") {
      await save(message);
    } else if (message.type === "empty") {
      await setStatus("empty", {
        kind: message.kind,
        message: "No history rows appeared within 10 seconds, so nothing was saved.",
      });
    }
  } catch (error) {
    await setStatus("error", { kind: message.kind, message: error.message });
  }
}

api.runtime.onMessage.addListener((message) => {
  queue = queue.then(() => handle(message)).catch(() => {});
});
