/*
 * A small MCP client for SHIP's Relay server, over Streamable HTTP.
 *
 * It does what any MCP client does, in order: initialize, announce that
 * it is initialized, list the server's tools, then call them. The Alexa+
 * simulator uses it so the page behaves like a real client of the server
 * instead of jumping straight to a tool call.
 *
 * Works in a browser (exposes window.ShipMcpClient) and in Node 18+
 * (module.exports), so the same code can be checked against the live server.
 */
(function (root) {
  "use strict";

  var PROTOCOL_VERSION = "2025-11-25";

  function ShipMcpClient(url, clientName) {
    this.url = url;
    this.clientName = clientName || "ship-mcp-client";
    this.nextId = 0;
    this.protocolVersion = null;   // set by the server's initialize reply
    this.sessionId = null;         // only if the server issues one
    this.serverInfo = null;
    this.tools = [];
    this._connecting = null;
  }

  // Streamable HTTP lets a server answer with plain JSON or an event stream.
  // Relay answers with JSON, but a conforming client accepts either.
  async function readBody(res) {
    var type = res.headers.get("content-type") || "";
    var text = await res.text();
    if (!text) return null;
    if (type.indexOf("text/event-stream") !== -1) {
      var last = null;
      text.split("\n").forEach(function (line) {
        if (line.indexOf("data:") === 0) last = line.slice(5).trim();
      });
      return last ? JSON.parse(last) : null;
    }
    return JSON.parse(text);
  }

  ShipMcpClient.prototype._headers = function () {
    var h = {
      "content-type": "application/json",
      "accept": "application/json, text/event-stream"
    };
    // After initialize the spec asks for the negotiated version on every request.
    if (this.protocolVersion) h["mcp-protocol-version"] = this.protocolVersion;
    if (this.sessionId) h["mcp-session-id"] = this.sessionId;
    return h;
  };

  ShipMcpClient.prototype._request = async function (method, params) {
    this.nextId += 1;
    var res = await fetch(this.url, {
      method: "POST",
      headers: this._headers(),
      body: JSON.stringify({ jsonrpc: "2.0", id: this.nextId, method: method, params: params || {} })
    });
    if (!res.ok) throw new Error("Relay returned HTTP " + res.status + " for " + method);
    var sid = res.headers.get("mcp-session-id");
    if (sid) this.sessionId = sid;
    var data = await readBody(res);
    if (!data) throw new Error("Relay sent an empty reply to " + method);
    if (data.error) throw new Error(data.error.message || ("Relay returned an error for " + method));
    return data.result;
  };

  ShipMcpClient.prototype._notify = async function (method) {
    var res = await fetch(this.url, {
      method: "POST",
      headers: this._headers(),
      body: JSON.stringify({ jsonrpc: "2.0", method: method })
    });
    if (!res.ok) throw new Error("Relay returned HTTP " + res.status + " for " + method);
  };

  // Runs the handshake once. Safe to call many times; a failed attempt can be retried.
  ShipMcpClient.prototype.connect = function () {
    var self = this;
    if (!this._connecting) {
      this._connecting = (async function () {
        var init = await self._request("initialize", {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: self.clientName, version: "1.0" }
        });
        self.protocolVersion = init.protocolVersion;
        self.serverInfo = init.serverInfo || null;
        await self._notify("notifications/initialized");
        var listed = await self._request("tools/list");
        self.tools = (listed.tools || []).map(function (t) { return t.name; });
        return self;
      })().catch(function (err) {
        self._connecting = null;   // allow a retry on the next call
        throw err;
      });
    }
    return this._connecting;
  };

  // Returns { text, payload }. A dict-returning tool arrives as JSON text,
  // so payload is the parsed object; a plain-string tool leaves payload as text.
  ShipMcpClient.prototype.callTool = async function (name, args) {
    await this.connect();
    if (this.tools.indexOf(name) === -1) throw new Error("Relay does not list a tool named " + name);
    var result = await this._request("tools/call", { name: name, arguments: args || {} });
    var first = result && result.content && result.content[0];
    var text = first && first.text;
    var payload;
    try { payload = JSON.parse(text); } catch (e) { payload = text; }
    return { text: text, payload: payload };
  };

  if (typeof module !== "undefined" && module.exports) module.exports = { ShipMcpClient: ShipMcpClient };
  else root.ShipMcpClient = ShipMcpClient;
})(typeof window !== "undefined" ? window : this);
