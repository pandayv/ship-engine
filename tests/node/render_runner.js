// Runs shipRenderFindings from docs/ship-render.js against a payload read from the file in argv[2],
// with a tiny DOM stub, and prints what was rendered as JSON. Used by tests/test_contracts.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const elements = {};
const stub = () => ({ id: "", className: "", textContent: "", innerHTML: "", children: [],
  appendChild(child) { this.children.push(child); return child; } });
const context = {
  URL,
  document: {
    head: stub(),
    getElementById: (id) => elements[id] || null,
    createElement: () => stub(),
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "..", "docs", "ship-render.js"), "utf8"), context);

const { payload, opts, label } = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const bar = stub();
const body = stub();
context.__args = { bar, body, payload, opts, label };
vm.runInContext("shipRenderFindings(__args.bar, __args.body, __args.label, __args.payload, __args.opts)", context);
process.stdout.write(JSON.stringify({
  bar: bar.textContent,
  html: body.innerHTML,
  appended: body.children.map((c) => ({ className: c.className, innerHTML: c.innerHTML })),
  escapes: context.shipEsc("<&\"'>"),
}));
