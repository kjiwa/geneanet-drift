"use strict";

(async () => {
  const api = globalThis.browser ?? globalThis.chrome;
  const WAIT_MS = 10000;
  const SETTLE_MS = 1000;

  // Rows render incrementally: wait for the first one, then for the page to stop changing.
  function waitForEntries() {
    const present = () => document.querySelector("gw-history-editing-entry") !== null;
    return new Promise((resolve) => {
      let quiet = null;
      const observer = new MutationObserver(() => {
        if (present()) {
          clearTimeout(quiet);
          quiet = setTimeout(() => finish(true), SETTLE_MS);
        }
      });
      const timer = setTimeout(() => finish(present()), WAIT_MS);
      function finish(found) {
        observer.disconnect();
        clearTimeout(timer);
        clearTimeout(quiet);
        resolve(found);
      }
      observer.observe(document.documentElement, { childList: true, subtree: true });
      if (present()) {
        quiet = setTimeout(() => finish(true), SETTLE_MS);
      }
    });
  }

  function describePerson(page, html) {
    if (page.kind !== "person") {
      return { name: null, n: null, p: null, oc: null };
    }
    return { name: capture.personName(html), ...capture.personIdentity(location.href, html) };
  }

  const { tree } = await api.storage.local.get("tree");
  if (!tree || capture.treeOf(location.href) !== tree) {
    return;
  }
  const isHistory = /_w\/history(\/editing)?\/?$/.test(location.pathname);
  if (isHistory && !(await waitForEntries())) {
    api.runtime.sendMessage({ type: "empty", kind: "history" });
    return;
  }
  const html = document.documentElement.outerHTML;
  const page = capture.classify(location.href, html);
  if (page === null) {
    return;
  }
  api.runtime.sendMessage({
    type: "capture",
    tree,
    ...page,
    ...describePerson(page, html),
    html: capture.stripForCapture(html),
  });
})();
