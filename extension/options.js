"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const ORIGINS = { origins: ["https://gw.geneanet.org/*"] };

const form = document.getElementById("form");
const treeInput = document.getElementById("tree");
const treeError = document.getElementById("tree-error");
const syncInput = document.getElementById("last-sync");
const saved = document.getElementById("saved");
const access = document.getElementById("access");

function showTreeError(message) {
  treeError.textContent = message;
  treeError.hidden = message === "";
  treeInput.setAttribute("aria-invalid", message === "" ? "false" : "true");
}

function showSaved(text) {
  saved.textContent = text;
  saved.hidden = text === "";
}

async function showAccess() {
  access.hidden = await api.permissions.contains(ORIGINS);
}

async function load() {
  const { tree = "", lastSync = "" } = await api.storage.local.get(["tree", "lastSync"]);
  treeInput.value = tree;
  syncInput.value = lastSync;
  await showAccess();
}

async function save(event) {
  event.preventDefault();
  showSaved("");
  const tree = capture.normalizeTree(treeInput.value);
  if (!capture.isValidTree(tree)) {
    showTreeError(
      tree === ""
        ? "Enter the tree name."
        : `"${tree}" is not a tree name. Use letters, digits, hyphens and underscores, starting with a letter or digit.`,
    );
    treeInput.focus();
    return;
  }
  showTreeError("");
  treeInput.value = tree;
  await api.storage.local.set({ tree, lastSync: syncInput.value });
  showSaved(`Saved. Pages of the tree "${tree}" are now saved when you open them.`);
}

async function grant() {
  await api.permissions.request(ORIGINS);
  await showAccess();
}

async function reset() {
  await api.storage.local.set({ captures: {}, status: null });
  const done = document.getElementById("reset-done");
  done.textContent = "The list of saved pages is cleared. Your tree name and review date are unchanged.";
  done.hidden = false;
}

form.addEventListener("submit", save);
document.getElementById("reset").addEventListener("click", reset);
document.getElementById("grant").addEventListener("click", grant);
load();
