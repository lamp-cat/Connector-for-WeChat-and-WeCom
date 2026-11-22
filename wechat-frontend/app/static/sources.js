/* Shared source picker. Requests retain their source; old windows and responses are invalidated. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const store = window.ChatSource = {source: null, version: 0, revision: null};
  function apply(data) {
    const changed = store.source !== null && (store.source !== data.active || store.revision !== data.revision);
    store.source = data.active; store.revision = data.revision;
    document.querySelectorAll("[data-source-picker]").forEach(n => n.value = store.source);
    document.querySelectorAll("[data-source-label]").forEach(n => n.textContent = data.labels[store.source]);
    if (changed) { store.version++; window.dispatchEvent(new Event("chat-source-change")); }
  }
  store.ready = originalFetch("/api/sources", {credentials: "same-origin"}).then(async r => {
    if (!r.ok) throw new Error("来源设置未能加载");
    apply(await r.json());
  });
  window.fetch = async (url, options = {}) => {
    if (typeof url !== "string" || !url.startsWith("/api/") || url.startsWith("/api/sources")) {
      return originalFetch(url, options);
    }
    await store.ready;
    const version = store.version;
    const headers = new Headers(options.headers || {});
    headers.set("X-Chat-Source", store.source);
    const result = await originalFetch(url, {...options, headers});
    if (version !== store.version) {
      const error = new Error("来源已变化，旧响应已丢弃"); error.name = "StaleSource"; throw error;
    }
    for (const method of ["json", "text", "blob", "arrayBuffer"]) {
      const read = result[method].bind(result);
      result[method] = async () => {
        const body = await read();
        if (version !== store.version) {
          const error = new Error("来源已变化，旧响应已丢弃"); error.name = "StaleSource"; throw error;
        }
        return body;
      };
    }
    return result;
  };
  document.querySelectorAll("[data-source-picker]").forEach(n => n.addEventListener("change", async () => {
    n.disabled = true;
    try {
      const r = await originalFetch("/api/sources", {method: "PUT", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({source: n.value})});
      if (!r.ok) throw new Error("来源切换失败");
      apply(await r.json());
    } catch (e) { n.value = store.source; alert(e.message); }
    finally { n.disabled = false; }
  }));
  setInterval(async () => {
    if (document.hidden) return;
    try { const r = await originalFetch("/api/sources", {credentials: "same-origin"}); if (r.ok) apply(await r.json()); }
    catch (_) {}
  }, 4000);
})();
