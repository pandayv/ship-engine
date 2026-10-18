// Loads the inline script of docs/ship-display.html in a vm with a small browser stub, plays a scripted
// sequence of server messages and visibility changes into it, and prints what the page did as JSON.
// Used by tests/test_display_page.py. The script argument is a JSON list of steps.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const docs = path.join(__dirname, "..", "..", "docs");
const html = fs.readFileSync(path.join(docs, "ship-display.html"), "utf8");
const inline = html.slice(html.lastIndexOf("<script>") + 8, html.lastIndexOf("</script>"));

function element(id) {
  const listeners = {};
  return {
    id, hidden: false, textContent: "", innerHTML: "", className: "", value: "", disabled: false, dataset: {},
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {}, focus() {},
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev)); },
  };
}

const elements = {};
const getEl = (id) => elements[id] || (elements[id] = element(id));
const docListeners = {};
const sockets = [];
const timers = [];

class FakeSocket {
  constructor(url) { this.url = url; this.sent = []; this.readyState = 0; this.l = {}; sockets.push(this); }
  addEventListener(type, fn) { (this.l[type] = this.l[type] || []).push(fn); }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; (this.l.close || []).forEach((fn) => fn({})); }
  open() { this.readyState = 1; (this.l.open || []).forEach((fn) => fn({})); }
  receive(raw) { (this.l.message || []).forEach((fn) => fn({ data: raw })); }
}
FakeSocket.OPEN = 1; FakeSocket.CLOSED = 3;

const store = { ship_display_device_name: "laptop" };
const consoleLog = { warn: [], error: [] };
const context = {
  URL, JSON, Uint8Array, atob,
  console: { warn: (...a) => consoleLog.warn.push(a.join(" ")), error: (...a) => consoleLog.error.push(a.join(" ")), log() {} },
  WebSocket: FakeSocket,
  localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
  navigator: {},
  Notification: { permission: "default" },
  document: {
    visibilityState: "visible", head: element("head"),
    getElementById: getEl,
    createElement: () => element("created"),
    querySelectorAll: () => [],
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  },
  setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  setTimeout: (fn) => 0,
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(docs, "ship-render.js"), "utf8"), context);
vm.runInContext(inline, context);

const steps = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const outcome = { threw: null };
try {
  for (const step of steps) {
    const sock = sockets[sockets.length - 1];
    if (step.open) sock.open();
    if (step.raw !== undefined) sock.receive(step.raw);
    if (step.message !== undefined) sock.receive(JSON.stringify(step.message));
    if (step.visibility) {
      context.document.visibilityState = step.visibility;
      (docListeners.visibilitychange || []).forEach((fn) => fn({}));
    }
    if (step.tick) timers.forEach((t) => t.fn());
  }
} catch (e) {
  outcome.threw = String(e && e.stack || e);
}
const sock = sockets[sockets.length - 1];
process.stdout.write(JSON.stringify({
  ...outcome,
  sent: sockets.flatMap((s) => s.sent),
  status: getEl("statusLine").innerHTML,
  label: getEl("stateLabel").textContent,
  dot: getEl("dot").className,
  nameError: getEl("nameError").textContent,
  nameboxHidden: getEl("namebox").hidden,
  body: getEl("screenBody").innerHTML,
  keepaliveMs: timers.map((t) => t.ms),
  sockets: sockets.length,
  warnings: consoleLog.warn.length,
  errors: consoleLog.error,
}));
