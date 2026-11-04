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
  await api.downloads.download({
    url: DATA_URL + encodeURIComponent(message.html),
    filename: plan.file,
    conflictAction: "overwrite",
    saveAs: false,
  });
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
