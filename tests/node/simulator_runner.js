// Runs the inline script of docs/alexa-simulator.html against a scripted fake Relay and prints
// which tools it called and what it showed. Used by tests/test_simulator_page.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const docs = path.join(__dirname, "..", "..", "docs");
const html = fs.readFileSync(path.join(docs, "alexa-simulator.html"), "utf8");
const inline = html.slice(html.lastIndexOf("<script>") + 8, html.lastIndexOf("</script>"));
const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));   // {tools: {name: reply}, actions: [...]}

function element(id) {
  const listeners = {};
  const el = {
    id, hidden: false, textContent: "", innerHTML: "", className: "", value: "", disabled: false, dataset: {}, children: [],
    classList: { _on: new Set(), add(c) { this._on.add(c); }, remove(c) { this._on.delete(c); }, toggle(c, on) { on ? this._on.add(c) : this._on.delete(c); }, contains(c) { return this._on.has(c); } },
    appendChild(child) { this.children.push(child); return child; },
    remove() {},
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type, ev) { return Promise.all((listeners[type] || []).map((fn) => fn(ev))); },
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text || ""; },
    set(v) { this._text = v; if (v === "") this.children = []; },
  });
  return el;
}
const elements = {};
const getEl = (id) => elements[id] || (elements[id] = element(id));
const bubbles = [];

const calls = [];
async function fakeFetch(url, init) {
  const body = JSON.parse(init.body);
  const reply = (result) => ({ ok: true, headers: { get: () => "application/json" }, text: async () => JSON.stringify({ jsonrpc: "2.0", id: body.id, result }) });
  if (body.method === "initialize") return reply({ protocolVersion: "2025-11-25", serverInfo: { name: "ship-relay", version: "1" } });
  if (body.method === "notifications/initialized") return { ok: true, headers: { get: () => "" }, text: async () => "" };
  if (body.method === "tools/list") return reply({ tools: Object.keys(scenario.tools).map((name) => ({ name })) });
  if (body.method === "tools/call") {
    calls.push({ name: body.params.name, args: body.params.arguments });
    const r = scenario.tools[body.params.name];
    const text = typeof r === "string" ? r : JSON.stringify(r);
    return reply({ content: [{ type: "text", text }], isError: r && r.__isError ? true : undefined });
  }
  throw new Error("unexpected " + body.method);
}

const context = {
  URL, JSON, fetch: fakeFetch, Promise, console,
  document: {
    head: element("head"), getElementById: getEl,
    createElement: () => element("created"),
    querySelectorAll: () => [],
  },
  window: {},
};
context.window = context;
context.document.createElement = (tag) => { const e = element(tag); return e; };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(docs, "ship-render.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(docs, "ship-mcp-client.js"), "utf8"), context);
vm.runInContext(inline, context);

(async () => {
  for (const action of scenario.actions) {
    if (action.ask) { getEl("askInput").value = action.ask; await getEl("askBtn").fire("click", {}); }
    if (action.show) {
      const target = { closest: () => ({ dataset: { surface: action.show } }) };
      await getEl("surfaceOffer").fire("click", { target });
      await new Promise((resolve) => setTimeout(resolve, 50));     // the page's handler does not return its promise
    }
  }
  const offer = getEl("surfaceOffer");
  process.stdout.write(JSON.stringify({
    calls,
    offered: offer.children.map((b) => b.dataset.surface),
    offerHtml: html.slice(html.indexOf('id="surfaceOffer"'), html.indexOf('id="surfaceOffer"') + 80),
    transcript: getEl("transcript").children.map((c) => c.innerHTML),
    deviceStatus: getEl("deviceStatus").textContent,
    screenBody: getEl("screenBody").innerHTML,
    ring: getEl("ring").classList.contains("alert"),
  }));
})().catch((e) => { console.error(e); process.exit(1); });
