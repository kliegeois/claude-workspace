// peanut-review settings: enable agents and pick models in the project review config.

(function () {
  const BASE = (typeof window.PR_BASE_URL === "string") ? window.PR_BASE_URL : "";
  const container = document.getElementById("settings-configs");
  const datalist = document.getElementById("model-options");

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function attrEsc(s) {
    return esc(s).replace(/"/g, "&quot;");
  }

  async function loadModels(refresh) {
    try {
      const r = await fetch(`${BASE}/api/models${refresh ? "?refresh=1" : ""}`, { cache: "no-store" });
      const data = await r.json();
      datalist.innerHTML = (data.models || [])
        .map((m) => `<option value="${attrEsc(m.id)}">${esc(m.label)}</option>`).join("");
    } catch { /* free text still works */ }
  }

  function renderConfig(config, index) {
    if (config.error) {
      return `<section class="settings-config"><h3 class="mono">${esc(config.path)}</h3>`
        + `<p class="error">${esc(config.error)}</p></section>`;
    }
    const rows = config.agents.map((a) => `<tr>`
      + `<td><input type="checkbox" data-enabled="${attrEsc(a.name)}"${a.enabled ? " checked" : ""} aria-label="Enable ${attrEsc(a.name)}"></td>`
      + `<td class="agent-name">${esc(a.name)}</td>`
      + `<td>${esc(a.role)}</td>`
      + `<td class="mono">${esc(a.persona || "—")}</td>`
      + `<td class="mono">${esc(a.runner)}</td>`
      + `<td><input class="mono settings-model" list="model-options" data-model="${attrEsc(a.name)}" value="${attrEsc(a.model)}" aria-label="${attrEsc(a.name)} model"></td>`
      + `</tr>`).join("");
    const hasEditor = config.agents.some((a) => a.role === "editor");
    const addEditor = hasEditor ? "" : `<label class="settings-add-editor"><input type="checkbox" data-add-editor> `
      + `Add an Editor agent so local reviews can make edits between rounds</label>`;
    return `<section class="settings-config" data-index="${index}">`
      + `<h3 class="mono">${esc(config.path)}</h3>`
      + `<table class="sessions settings-table"><thead><tr><th>On</th><th>Agent</th><th>Role</th><th>Persona</th><th>Runner</th><th>Model</th></tr></thead>`
      + `<tbody>${rows}</tbody></table>`
      + addEditor
      + `<p class="settings-status" role="status"></p>`
      + `<button type="button" class="primary settings-save">Save</button>`
      + `</section>`;
  }

  let configs = [];

  async function load() {
    try {
      const r = await fetch(`${BASE}/api/settings`, { cache: "no-store" });
      const data = await r.json();
      configs = data.configs || [];
    } catch (err) {
      container.innerHTML = `<p class="error">Cannot load settings: ${esc(err.message)}</p>`;
      return;
    }
    container.innerHTML = configs.length
      ? configs.map(renderConfig).join("")
      : `<p class="meta">No .peanut-review.json found. Start the server with <code>--review-config PATH</code>.</p>`;
  }

  async function save(section) {
    const config = configs[Number(section.dataset.index)];
    const status = section.querySelector(".settings-status");
    const button = section.querySelector(".settings-save");
    const agents = config.agents.map((a) => ({
      name: a.name,
      enabled: section.querySelector(`[data-enabled="${CSS.escape(a.name)}"]`).checked,
      model: section.querySelector(`[data-model="${CSS.escape(a.name)}"]`).value.trim(),
    }));
    const addEditor = Boolean(section.querySelector("[data-add-editor]")?.checked);
    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const r = await fetch(`${BASE}/api/settings/agents`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Peanut-Queue-Token": window.PR_ACTION_TOKEN || "" },
        body: JSON.stringify({ config: config.path, agents, add_editor: addEditor }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `Save failed (${r.status})`);
      await load();
      const fresh = container.querySelector(`[data-index="${section.dataset.index}"] .settings-status`);
      if (fresh) fresh.textContent = "Saved. New sessions use these agents.";
    } catch (err) {
      status.textContent = err.message;
      status.classList.add("error");
      button.disabled = false;
    }
  }

  container.addEventListener("click", (event) => {
    const button = event.target.closest(".settings-save");
    if (button) save(button.closest(".settings-config"));
  });
  document.getElementById("models-refresh")?.addEventListener("click", () => loadModels(true));
  loadModels(false);
  load();
})();
