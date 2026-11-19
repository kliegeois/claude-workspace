// Local review queue. Review tasks are handed to the user's driver agent.
(() => {
  "use strict";
  const base = window.PR_BASE_URL || "";
  const byId = (id) => document.getElementById(id);
  let data = { items: [], accounts: [] };
  let loading = false;
  let submitting = false;
  const themes = ["system", "dark-plus", "light"];
  let theme = "system";
  try { theme = localStorage.getItem("pr.theme") || "system"; } catch { /* optional */ }
  function applyTheme() {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = theme;
    byId("theme-toggle").textContent = `theme: ${theme}`;
  }
  byId("theme-toggle").addEventListener("click", () => {
    theme = themes[(themes.indexOf(theme) + 1) % themes.length];
    try { localStorage.setItem("pr.theme", theme); } catch { /* optional */ }
    applyTheme();
  });
  applyTheme();

  const HIDE_KEY = "pr.queue.hide";
  const toggles = { approved: byId("queue-hide-approved"), drafts: byId("queue-hide-drafts"), stale: byId("queue-hide-stale") };
  try {
    const saved = JSON.parse(localStorage.getItem(HIDE_KEY) || "{}");
    for (const [name, box] of Object.entries(toggles)) box.checked = saved[name] === true;
  } catch { /* optional */ }
  function saveToggles() {
    const prefs = Object.fromEntries(Object.entries(toggles).map(([name, box]) => [name, box.checked]));
    try { localStorage.setItem(HIDE_KEY, JSON.stringify(prefs)); } catch { /* optional */ }
  }

  const REPOS_KEY = "pr.queue.repos";
  let repos = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(REPOS_KEY) || "[]");
    if (Array.isArray(saved)) repos = new Set(saved.filter((repo) => typeof repo === "string"));
  } catch { /* optional */ }
  function saveRepos() {
    try { localStorage.setItem(REPOS_KEY, JSON.stringify([...repos])); } catch { /* optional */ }
  }
  let repoChoices = null;
  function renderRepos() {
    // Keep saved repositories listed even when they currently have no PRs.
    const names = new Map();
    for (const item of data.items) names.set(item.repo.toLowerCase(), item.repo);
    for (const repo of repos) if (!names.has(repo)) names.set(repo, repo);
    const sorted = [...names.entries()].sort(([a], [b]) => a.localeCompare(b));
    const choices = sorted.map(([repo]) => repo).join("\n");
    if (choices !== repoChoices) {
      // Rebuild only when the set changes so polling does not disturb an open list.
      repoChoices = choices;
      const list = byId("queue-repos-list");
      list.replaceChildren();
      for (const [repo, label] of sorted) {
        const box = node("input");
        box.type = "checkbox";
        box.checked = repos.has(repo);
        box.addEventListener("change", () => {
          if (box.checked) repos.add(repo); else repos.delete(repo);
          saveRepos();
          render();
        });
        const row = node("label", null, "queue-toggle");
        row.append(box, ` ${label}`);
        list.append(row);
      }
    }
    byId("queue-repos-summary").textContent = repos.size === 0 ? "All repositories"
      : repos.size === 1 ? names.get([...repos][0]) : `${repos.size} repositories`;
  }

  function node(tag, text, className = "") {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function relativeTime(value) {
    if (!value) return "Never checked";
    const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
    if (seconds < 60) return "Checked just now";
    if (seconds < 3600) return `Checked ${Math.floor(seconds / 60)}m ago`;
    return `Checked ${Math.floor(seconds / 3600)}h ago`;
  }
  function error(message) {
    byId("queue-error").textContent = message;
    byId("queue-error").hidden = !message;
  }
  async function copyTask(item) {
    try {
      await navigator.clipboard.writeText(item.driver_task);
      byId("queue-copy-status").textContent = "Task copied. Paste it into your driver conversation.";
    } catch {
      const text = byId("queue-task-text");
      text.value = item.driver_task;
      byId("queue-task-dialog").showModal();
      text.focus();
      text.select();
    }
  }
  function visible(item) {
    const query = byId("queue-search").value.trim().toLowerCase();
    const account = byId("queue-account").value;
    if (account && item.account_key !== account) return false;
    if (repos.size && !repos.has(item.repo.toLowerCase())) return false;
    const requestType = byId("queue-request-type").value;
    if (requestType && requestType !== item.request_kind) return false;
    if (query && !`${item.repo} ${item.number} ${item.title || ""} ${item.author || ""}`.toLowerCase().includes(query)) return false;
    if (toggles.approved.checked && item.approved) return false;
    if (toggles.drafts.checked && item.draft) return false;
    if (toggles.stale.checked && item.freshness === "stale") return false;
    const filter = byId("queue-filter").value;
    if (filter !== "all" && item.state && item.state !== "open") return false;
    if (filter === "requested") return item.requested;
    if (filter === "stale") return item.freshness === "stale";
    if (filter === "running") return item.session_progress?.status === "running";
    if (filter === "attention") return item.requested || item.freshness !== "current" || item.session_progress?.status === "running" || item.session_progress?.status === "failed";
    return true;
  }
  function render() {
    const accounts = byId("queue-accounts");
    accounts.replaceChildren();
    const selected = byId("queue-account").value;
    byId("queue-account").replaceChildren(new Option("All accounts", ""));
    for (const account of data.accounts) {
      byId("queue-account").add(new Option(account.label, account.key));
      const card = node("div", null, `queue-account-card${account.error ? " account-error" : ""}`);
      card.append(node("strong", account.label), node("span", `@${account.login} · ${account.hostname}`, "meta"));
      card.append(node("span", account.error || (data.refreshing ? "Checking GitHub…" : relativeTime(account.checked_at)), account.error ? "error" : "meta"));
      accounts.append(card);
    }
    byId("queue-account").value = selected;
    renderRepos();
    const rows = data.items.filter(visible);
    byId("queue-count").textContent = `${rows.length} of ${data.items.length} PRs`;
    byId("queue-refresh").disabled = submitting || data.refreshing;
    byId("queue-refresh").textContent = data.refreshing ? "Refreshing…" : "Refresh queue";
    const body = byId("queue-rows");
    body.replaceChildren();
    for (const item of rows) {
      const row = node("tr", null, "session-row");
      const pr = node("td", null, "queue-pr");
      const link = node("a", item.title || `${item.repo} #${item.number}`);
      // Construct URLs from validated identity fields, never arbitrary PR text.
      link.href = `https://${item.account.hostname}/${item.repo}/pull/${item.number}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      pr.append(link, node("div", `${item.repo} #${item.number}${item.author ? ` · ${item.author}` : ""}${item.draft ? " · Draft" : ""}${item.state && item.state !== "open" ? ` · ${item.state}` : ""}`, "sub"));
      const identity = node("td");
      const account = data.accounts.find((a) => a.key === item.account_key);
      const requestLabel = { direct: "Direct request", team: "Team request" }[item.request_kind] || "No known request";
      identity.append(node("div", account?.label || item.account.login), node("div", requestLabel, "sub"));
      if (item.requested) identity.append(node("div", "Review requested", "sub"));
      else if (item.request_kind) identity.append(node("div", "No pending request", "sub"));
      if (item.approved) identity.append(node("div", "Approved by you", "sub"));
      if (item.request_kind_error) identity.append(node("div", item.request_kind_error, "queue-detail error"));
      const review = node("td");
      const progress = item.session_progress?.status || "pending";
      review.append(node("span", item.session_progress?.label || "Not started", `badge review-progress progress-${progress}`));
      const freshness = node("td");
      const freshLabels = { current: "Up to date", stale: "Stale", unknown: "Unknown", unreviewed: "Not reviewed", incomplete: "Completion unconfirmed" };
      freshness.append(node("span", freshLabels[item.freshness], `queue-freshness freshness-${item.freshness}`));
      const detail = item.completed_snapshot ? `Reviewed ${item.completed_snapshot.head_sha.slice(0, 8)} · latest ${(item.head_sha || "").slice(0, 8)}` : item.head_sha ? `Latest ${item.head_sha.slice(0, 8)}` : "";
      freshness.append(node("div", detail, "sub mono"), node("div", relativeTime(item.checked_at), "sub"));
      if (item.completion_detail) freshness.append(node("div", item.completion_detail, "queue-detail"));
      if (item.error) freshness.append(node("div", item.error, "queue-detail error"));
      const actions = node("td", null, "queue-actions");
      if (item.session_id) {
        const open = node("a", "Open review", "queue-open");
        open.href = `${base}/${encodeURIComponent(item.session_id)}`;
        actions.append(open);
      }
      if (!item.state || item.state === "open") {
        const copy = node("button", item.session_id ? "Copy re-review task" : "Copy review task");
        copy.type = "button";
        copy.disabled = !item.driver_task;
        copy.addEventListener("click", () => copyTask(item));
        actions.append(copy);
      }
      row.append(pr, identity, review, freshness, actions);
      body.append(row);
    }
    byId("queue-empty").hidden = rows.length > 0;
    byId("queue-empty").textContent = data.refreshing && !data.items.length ? "Checking your accounts for review requests…" : data.accounts.some((a) => a.error) && !data.items.length ? "Could not load every account. See account details above and refresh to retry." : "No PRs match this view.";
    document.querySelector(".queue-table").hidden = rows.length === 0;
  }
  async function refresh() {
    if (loading || !window.PR_QUEUE_ENABLED) return;
    loading = true;
    try {
      const response = await fetch(`${base}/api/queue`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load queue (${response.status})`);
      data = await response.json();
      render();
    } catch (err) { error(err.message); }
    finally { loading = false; }
  }
  async function action(name, body = {}) {
    submitting = true;
    error("");
    render();
    try {
      const response = await fetch(`${base}/api/queue/${name}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Peanut-Queue-Token": window.PR_QUEUE_TOKEN },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Action failed (${response.status})`);
    } catch (err) { error(err.message); }
    finally { submitting = false; await refresh(); render(); }
  }
  byId("queue-refresh").addEventListener("click", () => action("refresh"));
  byId("queue-task-close").addEventListener("click", () => byId("queue-task-dialog").close());
  byId("queue-search").addEventListener("input", render);
  byId("queue-account").addEventListener("change", render);
  byId("queue-request-type").addEventListener("change", render);
  byId("queue-filter").addEventListener("change", render);
  for (const box of Object.values(toggles)) box.addEventListener("change", () => { saveToggles(); render(); });
  byId("queue-repos-clear").addEventListener("click", () => {
    repos.clear();
    repoChoices = null;
    saveRepos();
    render();
  });
  document.addEventListener("click", (event) => {
    if (!byId("queue-repos").contains(event.target)) byId("queue-repos").open = false;
  });
  if (window.PR_QUEUE_ENABLED) {
    refresh();
    setInterval(refresh, 3000);
  } else byId("queue-refresh").disabled = true;
})();
