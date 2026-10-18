// Runs docs/ship-sw.js in a vm with a fake service-worker scope, fires pushsubscriptionchange and prints what happened.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const mode = process.argv[2];       // "with-key" | "no-key" | "subscribe-fails"
const handlers = {};
const posted = [];
const subscribeCalls = [];
const self = {
  addEventListener: (type, fn) => { handlers[type] = fn; },
  skipWaiting() {}, clients: { claim() {}, matchAll: async () => [{ postMessage: (m) => posted.push(m) }] },
  registration: {
    scope: "https://x.example/", showNotification: async () => {},
    pushManager: { subscribe: async (opts) => { subscribeCalls.push(opts); if (mode === "subscribe-fails") throw new Error("denied"); return {}; } },
  },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "..", "docs", "ship-sw.js"), "utf8"), { self });

const KEY = new Uint8Array([1, 2, 3]);
const event = {
  oldSubscription: mode === "no-key" ? null : { options: { applicationServerKey: KEY } },
  waitUntil: (p) => { event.done = p; },
};
handlers.pushsubscriptionchange(event);
event.done.then(() => process.stdout.write(JSON.stringify({
  handlerRegistered: typeof handlers.pushsubscriptionchange === "function",
  subscribeCalls: subscribeCalls.map((o) => ({ userVisibleOnly: o.userVisibleOnly, sameKey: o.applicationServerKey === KEY })),
  posted,
})));
