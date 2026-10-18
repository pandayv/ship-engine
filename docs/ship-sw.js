"use strict";

// Wakes when SHIP sends a push to this browser, even with the display page closed.
// It shows a short notification and nothing more. Findings are never in the push:
// they arrive over the page's own connection once the person opens it.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let message = {};
  try { message = event.data ? event.data.json() : {}; } catch { /* show the default text */ }
  event.waitUntil(self.registration.showNotification(message.title || "SHIP", {
    body: message.body || "Findings need a decision. Tap to review.",
    tag: "ship-findings",          // a newer notification replaces the older one
    renotify: true,
    requireInteraction: true,      // stays until the person acts on it
    data: { url: message.url || self.registration.scope + "ship-display.html" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data.url;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const page = open.find((c) => c.url.includes("ship-display.html"));
    if (page) {
      await page.focus();
      page.postMessage({ type: "ship-opened" });   // tells an already-open page to collect what is waiting
      return;
    }
    await self.clients.openWindow(url);
  })());
});

// The browser can replace a push subscription (it expires, or site data is cleared). SHIP's copy of
// the old address then stops working. Take a new one under the same key and tell any open page, which
// files it with SHIP the next time it registers. The key is not repeated here: the browser hands
// the old subscription's own key to this event.
//
// If the browser gives no key, or subscribing fails, nothing is guessed. The page notices on its next
// visit that it has permission but no subscription and offers to turn alerts on again.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    const options = event.oldSubscription && event.oldSubscription.options;
    const key = options && options.applicationServerKey;
    let resubscribed = false;
    if (key) {
      try {
        await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
        resubscribed = true;
      } catch { /* the page offers to turn alerts on again */ }
    }
    const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    pages.forEach((page) => page.postMessage({ type: "ship-subscription-changed", resubscribed }));
  })());
});
