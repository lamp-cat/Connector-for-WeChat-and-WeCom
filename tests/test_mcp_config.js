/* Exercise the real settings script with a captured clipboard, never the system clipboard. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function check(source, server) {
  const nodes = new Map();
  function element() {
    const classes = new Set();
    return {
      textContent: "", children: [], dataset: {},
      classList: {add: x => classes.add(x), remove: x => classes.delete(x),
                  toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
      append(...children) {this.children.push(...children);},
      scrollIntoView() {},
    };
  }
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const copied = [];
  const info = {source, server_name: server, enabled: false,
    stdio: {command: "/fixture/reader", args: source === "wecom" ? ["--source", "wecom"] : ["mcp"], env: {}},
    http: {url: source === "wecom" ? "http://127.0.0.1:8088/mcp/wecom" : "http://127.0.0.1:5678/mcp", auth: "fixture"},
    tools: []};
  const status = {reader_bin_exists: true, tools: [
    {id: "doubao-work", label: "豆包工作", detected: true, manual: true, registered: false}]};
  const sandbox = {
    window: {}, document: {getElementById: get, createElement: element},
    navigator: {clipboard: {async writeText(text) {copied.push(text);}}},
    confirm: () => true,
    fetch: async url => ({status: 200, ok: true, json: async () => url === "/api/agents" ? status : info}),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../wechat-frontend/app/static/settings-agents.js"), "utf8"), sandbox);
  await new Promise(setImmediate);
  const row = get("agList").children[0];
  assert.equal(row.children[0].textContent, "豆包工作");
  assert.equal(row.children[1].textContent, "已检测到 · 手动接入");
  await row.children[2].onclick();
  assert.equal(get("doubaoServer").textContent, server);
  await get("btnDoubaoCopy").onclick();
  await get("btnCopyStdio").onclick();
  for (const text of copied) {
    const config = JSON.parse(text);
    assert.deepEqual(Object.keys(config.mcpServers), [server]);
    assert.deepEqual(config.mcpServers[server].args, info.stdio.args);
  }
}

(async () => {
  await check("personal", "wechat-reader");
  await check("wecom", "wecom-reader");
  console.log("PASS: both copy buttons preserve personal and enterprise MCP service identity");
})().catch(error => {console.error(error); process.exitCode = 1;});
