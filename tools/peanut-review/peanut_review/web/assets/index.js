// peanut-review session index: refresh-on-click + periodic rescan.

(function () {
  const BASE = (typeof window.PR_BASE_URL === "string") ? window.PR_BASE_URL : "";
  const SESSION_KIND = (typeof window.PR_SESSION_KIND === "string") ? window.PR_SESSION_KIND : "";
  const THEME_KEY = "pr.theme";
  const THEMES = [
    { value: "system", label: "system" },
    { value: "dark-plus", label: "Dark+" },
    { value: "light", label: "light" },
  ];

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function attrEsc(s) {
    return esc(s).replace(/"/g, "&quot;");
  }
  function themeConfig(value) {
    return THEMES.find((t) => t.value === value) || THEMES[0];
  }
  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "system";
    } catch {
      return "system";
    }
  }
  function applyTheme(value) {
    const theme = themeConfig(value);
    if (theme.value === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.dataset.theme = theme.value;
    }
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = `theme: ${theme.label}`;
      btn.title = `Color theme: ${theme.label}`;
      btn.dataset.theme = theme.value;
    }
  }
  function setStoredTheme(value) {
    const theme = themeConfig(value);
    try {
      if (theme.value === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme.value);
    } catch { /* ignore */ }
    applyTheme(theme.value);
  }
  function cycleTheme() {
    const current = themeConfig(storedTheme()).value;
    const idx = THEMES.findIndex((t) => t.value === current);
    setStoredTheme(THEMES[(idx + 1) % THEMES.length].value);
  }
  applyTheme(storedTheme());
  document.getElementById("theme-toggle")?.addEventListener("click", cycleTheme);

  function countsCell(s) {
    const parts = [
      `<span class="n">${s.comment_count}</span><span class="sub"> total</span>`,
    ];
    if (s.unresolved_count)
      parts.push(`<span class="n warn">${s.unresolved_count}</span><span class="sub"> open</span>`);
    if (s.stale_count)
      parts.push(`<span class="n muted">${s.stale_count}</span><span class="sub"> stale</span>`);
    const counts = parts.join(" · ");
    const activity = s.push_activity;
    if (!activity || !activity.label) return counts;
    if (activity.status !== "new" && activity.status !== "never") return counts;
    return counts
      + `<div class="push-activity push-${activity.status}" title="GitHub comment push activity">${esc(activity.label)}</div>`;
  }

  function relativeTimeLabel(timestamp) {
    const millis = Date.parse(timestamp);
    if (!Number.isFinite(millis)) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - millis) / 1000));
    if (seconds < 45) return "just now";
    if (seconds < 90) return "1 minute ago";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} minutes ago`;
    if (minutes < 90) return "1 hour ago";
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours !== 1 ? "s" : ""} ago`;
    if (hours < 48) return "yesterday";
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} days ago`;
    const months = Math.max(1, Math.floor(days / 30));
    if (days < 365) return `${months} month${months !== 1 ? "s" : ""} ago`;
    const years = Math.max(1, Math.floor(days / 365));
    return `${years} year${years !== 1 ? "s" : ""} ago`;
  }

  function updatedCell(timestamp) {
    const label = relativeTimeLabel(timestamp);
    if (!label) return esc(timestamp);
    return `<time class="session-updated" datetime="${esc(timestamp)}" title="${esc(timestamp)}">${esc(label)}</time>`;
  }

  function rowHtml(s) {
    const progress = s.progress || { label: "no review agents", status: "pending" };
    const change = s.change_label || `${s.base_ref} … ${s.topic_ref}`;
    const sessionSubtitle = s.session_subtitle || s.current_head || "";
    const sessionReference = s.github_url
      ? `<a class="github-ref" href="${attrEsc(s.github_url)}" target="_blank" rel="noopener noreferrer" title="Open GitHub PR">${esc(sessionSubtitle)}</a>`
      : esc(sessionSubtitle);
    const cleanup = s.cleanup || {};
    const safe = Boolean(cleanup.safe_to_delete);
    const cleanupHint = cleanup.label
      ? `<div class="cleanup-hint${safe ? " safe" : ""}">${esc(cleanup.label)}</div>`
      : "";
    const check = s.is_github
      ? `<button type="button" class="session-pr-check" data-id="${attrEsc(s.id)}" title="Check whether the PR was merged">Check PR</button>`
      : "";
    return `
      <tr class="session-row${safe ? " safe-to-delete" : ""}" data-id="${esc(s.id)}">
        <td class="id"><a href="${BASE}/${esc(s.id)}">${esc(s.id)}</a>
          <div class="mono head">${sessionReference}</div></td>
        <td><span class="badge review-progress progress-${esc(progress.status)}" title="Agent-derived review progress">${esc(progress.label)}</span>${cleanupHint}</td>
        <td class="change" title="${esc(change)}">${esc(change)}</td>
        <td class="mono workspace">${esc(s.workspace)}</td>
        <td class="counts">${countsCell(s)}</td>
        <td class="mono updated">${updatedCell(s.updated_at)}</td>
        <td class="session-actions">${check}<button type="button" class="session-delete" data-id="${attrEsc(s.id)}">Delete</button></td>
      </tr>
    `;
  }

  async function checkPr(button) {
    const id = button.dataset.id;
    button.disabled = true;
    button.textContent = "Checking…";
    try {
      const r = await fetch(`${BASE}/${encodeURIComponent(id)}/api/session/pr-state`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Peanut-Queue-Token": window.PR_ACTION_TOKEN || "" },
        body: "{}",
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `Check failed (${r.status})`);
      if (data.error) throw new Error(data.error);
      await refresh();
    } catch (err) {
      button.disabled = false;
      button.textContent = "Check PR";
      button.title = err.message;
    }
  }

  function formatBytes(bytes) {
    if (typeof bytes !== "number") return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(unit && value < 10 ? 1 : 0)} ${units[unit]}`;
  }

  const deleteDialog = document.getElementById("delete-dialog");
  const deleteBody = document.getElementById("delete-body");
  const deleteError = document.getElementById("delete-error");
  const deleteConfirm = document.getElementById("delete-confirm");
  let deleteTarget = null;
  let deletePlan = null;
  let deleteBusy = false;

  function showDeleteError(message) {
    deleteError.textContent = message || "";
    deleteError.hidden = !message;
  }

  function renderDeletePlan(plan) {
    deletePlan = plan;
    const s = plan.session;
    const w = plan.worktree;
    const size = formatBytes(s.size_bytes);
    let html = `<p>This permanently deletes <strong>${esc(s.id)}</strong>: its comments, notes, logs and builds.</p>`;
    html += `<p class="mono sub">${esc(s.path)}${size ? ` · ${esc(size)}` : ""}</p>`;
    if (s.github && s.unpublished_count) {
      html += `<p class="warn">${s.unpublished_count} comment(s) were never pushed to GitHub and will be lost.</p>`;
    } else if (!s.github && s.comment_count) {
      html += `<p class="warn">${s.comment_count} local review comment(s) will be lost.</p>`;
    }
    if (plan.blockers.length) {
      html += `<p class="error">This session cannot be deleted right now:</p><ul class="error">`
        + plan.blockers.map((b) => `<li>${esc(b)}</li>`).join("") + "</ul>";
    }
    const wtSize = formatBytes(w.size_bytes);
    html += `<label class="delete-worktree"><input id="delete-worktree" type="checkbox"${w.removable && plan.deletable ? "" : " disabled"}>`
      + ` Also remove the worktree <span class="mono">${esc(w.path)}</span>${wtSize ? ` · ${esc(wtSize)}` : ""}</label>`;
    if (!w.removable) html += `<p class="sub">Not available: ${esc(w.reason)}</p>`;
    for (const note of w.notes || []) html += `<p class="sub">${esc(note)}</p>`;
    if (w.removable && w.untracked.length) {
      const shown = w.untracked.slice(0, 20);
      html += `<div class="delete-untracked" hidden><p class="sub">These untracked files are deleted with the worktree:</p><ul class="mono sub">`
        + shown.map((p) => `<li>${esc(p)}</li>`).join("")
        + (w.untracked.length > shown.length ? `<li>… and ${w.untracked.length - shown.length} more</li>` : "")
        + "</ul></div>";
    }
    deleteBody.innerHTML = html;
    const box = document.getElementById("delete-worktree");
    const untracked = deleteBody.querySelector(".delete-untracked");
    const sync = () => {
      deleteConfirm.textContent = box.checked ? "Delete session and worktree" : "Delete session";
      if (untracked) untracked.hidden = !box.checked;
    };
    box.addEventListener("change", sync);
    sync();
    deleteConfirm.disabled = !plan.deletable;
  }

  async function openDeleteDialog(id) {
    deleteTarget = id;
    deletePlan = null;
    showDeleteError("");
    deleteConfirm.disabled = true;
    deleteConfirm.textContent = "Delete session";
    deleteBody.innerHTML = `<p>Checking whether <strong>${esc(id)}</strong> can be deleted…</p>`;
    deleteDialog.showModal();
    try {
      const r = await fetch(`${BASE}/${encodeURIComponent(id)}/api/session/delete-plan`, { cache: "no-store" });
      const data = await r.json();
      if (deleteTarget !== id) return;
      if (!r.ok) throw new Error(data.error || `Check failed (${r.status})`);
      renderDeletePlan(data);
    } catch (err) {
      if (deleteTarget === id) showDeleteError(err.message);
    }
  }

  async function confirmDelete() {
    if (!deletePlan || deleteBusy) return;
    const box = document.getElementById("delete-worktree");
    const removeWorktree = Boolean(box && box.checked);
    deleteBusy = true;
    deleteConfirm.disabled = true;
    deleteConfirm.textContent = "Deleting…";
    showDeleteError("");
    try {
      const r = await fetch(`${BASE}/${encodeURIComponent(deleteTarget)}/api/session/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Peanut-Queue-Token": window.PR_ACTION_TOKEN || "" },
        body: JSON.stringify({
          remove_worktree: removeWorktree,
          confirmed_untracked: removeWorktree ? deletePlan.worktree.untracked : [],
        }),
      });
      const data = await r.json();
      if (r.status === 409 && data.plan) {
        renderDeletePlan(data.plan);
        throw new Error(data.error);
      }
      if (!r.ok) throw new Error(data.error || `Delete failed (${r.status})`);
      deleteDialog.close();
      await refresh();
    } catch (err) {
      showDeleteError(err.message);
      if (deletePlan) deleteConfirm.disabled = !deletePlan.deletable;
    } finally {
      deleteBusy = false;
    }
  }

  document.getElementById("session-rows")?.addEventListener("click", (event) => {
    const button = event.target.closest(".session-delete");
    if (button) openDeleteDialog(button.dataset.id);
    const check = event.target.closest(".session-pr-check");
    if (check) checkPr(check);
  });
  document.getElementById("delete-cancel")?.addEventListener("click", () => { if (!deleteBusy) deleteDialog.close(); });
  deleteConfirm?.addEventListener("click", confirmDelete);
  deleteDialog?.addEventListener("cancel", (event) => { if (deleteBusy) event.preventDefault(); });
  deleteDialog?.addEventListener("close", () => { deleteTarget = null; });

  const PAGE_SIZE = 50;
  const pageEtags = new Map();
  let loaded = document.querySelectorAll("#session-rows .session-row").length;
  let activeQuery = "";
  let refreshInFlight = false;

  function applyPage(page, { append = false } = {}) {
    const sessions = Array.isArray(page) ? page : (page.sessions || []);
    const total = Array.isArray(page) ? sessions.length : Number(page.total || 0);
    const tbody = document.getElementById("session-rows");
    if (!tbody) return;
    const rendered = sessions.map(rowHtml).join("");
    if (append) tbody.insertAdjacentHTML("beforeend", rendered);
    else tbody.innerHTML = rendered;
    loaded = tbody.querySelectorAll(".session-row").length;
    const table = tbody.closest("table");
    const empty = document.getElementById("session-empty");
    if (table) table.hidden = loaded === 0;
    if (empty) empty.hidden = loaded !== 0;
    const meta = document.getElementById("session-count");
    if (meta) meta.textContent = `${total} session${total !== 1 ? "s" : ""}`;
    const more = document.getElementById("load-more");
    if (more) more.hidden = Array.isArray(page) || !page.has_more;
  }

  async function fetchPage(offset) {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (SESSION_KIND) params.set("kind", SESSION_KIND);
    if (activeQuery) params.set("q", activeQuery);
    const url = BASE + "/api/sessions?" + params.toString();
    const headers = {};
    const etag = pageEtags.get(url);
    if (etag) headers["If-None-Match"] = etag;
    const r = await fetch(url, {
      headers,
      cache: "no-store",
    });
    if (r.status === 304) return null;
    if (!r.ok) return null;
    const nextEtag = r.headers.get("ETag");
    if (nextEtag) pageEtags.set(url, nextEtag);
    return r.json();
  }

  async function refresh() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const page = await fetchPage(0);
      if (page) applyPage(page);
    } catch { /* ignore */ }
    finally { refreshInFlight = false; }
  }

  document.getElementById("refresh")?.addEventListener("click", refresh);
  document.getElementById("load-more")?.addEventListener("click", async () => {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const page = await fetchPage(loaded);
      if (page) applyPage(page, { append: true });
    } catch { /* ignore */ }
    finally { refreshInFlight = false; }
  });
  let searchTimer = null;
  const sessionSearch = document.getElementById("session-search");
  sessionSearch?.addEventListener("input", (event) => {
    activeQuery = String(event.target.value || "").trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refresh, 180);
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (event.key === "Escape" && target === sessionSearch) {
      event.preventDefault();
      sessionSearch.blur();
      return;
    }
    const tagName = target?.tagName;
    const isEditing = target?.isContentEditable
      || tagName === "INPUT"
      || tagName === "TEXTAREA"
      || tagName === "SELECT";
    if (event.key !== "/" || event.defaultPrevented || event.ctrlKey
        || event.metaKey || event.altKey || isEditing || !sessionSearch) return;
    event.preventDefault();
    sessionSearch.focus();
    sessionSearch.select();
  });
  setInterval(refresh, 15000);

  // New local review form (Local reviews tab only).
  const localForm = document.getElementById("local-new-form");
  const localAgents = document.getElementById("local-new-agents");
  const localError = document.getElementById("local-new-error");
  let settingsConfigs = [];

  function showLocalError(message) {
    if (!localError) return;
    localError.textContent = message || "";
    localError.hidden = !message;
  }

  function renderLocalAgents() {
    if (!localForm || !localAgents) return;
    const path = localForm.elements.config.value;
    const config = settingsConfigs.find((c) => c.path === path);
    const agents = config ? config.agents : [];
    if (!agents.length) {
      localAgents.innerHTML = `<legend>Agents</legend><p class="sub">${esc(config && config.error ? config.error : "No agents configured.")}</p>`;
      return;
    }
    const rows = agents.map((a) => `<label class="local-agent">`
      + `<input type="checkbox" data-agent="${attrEsc(a.name)}"${a.enabled ? " checked" : ""}> `
      + `<span class="agent-name">${esc(a.name)}</span> <span class="sub">${esc(a.role)}${a.persona ? " · " + esc(a.persona) : ""}</span> `
      + `<input class="mono local-agent-model" list="local-model-options" data-model="${attrEsc(a.name)}" value="${attrEsc(a.model)}" aria-label="${attrEsc(a.name)} model">`
      + `</label>`).join("");
    const hasEditor = agents.some((a) => a.role === "editor");
    const editorNote = hasEditor ? "" : `<p class="sub warn">No Editor agent in this config. Add one in Settings before starting a local review.</p>`;
    localAgents.innerHTML = `<legend>Agents</legend>${rows}${editorNote}<datalist id="local-model-options"></datalist>`;
    loadModelOptions(document.getElementById("local-model-options"));
  }

  async function loadModelOptions(datalist) {
    if (!datalist) return;
    try {
      const r = await fetch(`${BASE}/api/models`, { cache: "no-store" });
      const data = await r.json();
      datalist.innerHTML = (data.models || []).map((m) => `<option value="${attrEsc(m.id)}">${esc(m.label)}</option>`).join("");
    } catch { /* free text still works */ }
  }

  async function loadLocalSettings() {
    try {
      const r = await fetch(`${BASE}/api/settings`, { cache: "no-store" });
      const data = await r.json();
      settingsConfigs = data.configs || [];
    } catch (err) {
      settingsConfigs = [];
      showLocalError(`Cannot load agent settings: ${err.message}`);
    }
    renderLocalAgents();
  }

  async function startLocalReview(event) {
    event.preventDefault();
    showLocalError("");
    const submit = document.getElementById("local-new-submit");
    const agents = Array.from(localAgents.querySelectorAll("input[data-agent]")).map((box) => {
      const model = ((localAgents.querySelector(`input[data-model="${CSS.escape(box.dataset.agent)}"]`) || {}).value || "").trim();
      return model ? { name: box.dataset.agent, enabled: box.checked, model }
        : { name: box.dataset.agent, enabled: box.checked };
    });
    const body = {
      workspace: localForm.elements.workspace.value.trim(),
      base: localForm.elements.base.value.trim(),
      config: localForm.elements.config.value,
      rounds: Number(localForm.elements.rounds.value || 0),
      agents,
    };
    submit.disabled = true;
    submit.textContent = "Starting…";
    try {
      const r = await fetch(`${BASE}/api/local/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Peanut-Queue-Token": window.PR_ACTION_TOKEN || "" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok && !data.session_id) throw new Error(data.error || `Start failed (${r.status})`);
      if (!r.ok) alert(data.error);
      window.location.href = `${BASE}/${encodeURIComponent(data.session_id)}`;
    } catch (err) {
      showLocalError(err.message);
      submit.disabled = false;
      submit.textContent = "Start review";
    }
  }

  if (localForm) {
    localForm.elements.config.addEventListener("change", renderLocalAgents);
    localForm.addEventListener("submit", startLocalReview);
    loadLocalSettings();
  }
})();
