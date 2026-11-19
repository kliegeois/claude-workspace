"""Review queue page; data and actions are served by the existing HTTP app."""
from __future__ import annotations

import html
import json

from .render import FAVICON_HREF, THEME_BOOTSTRAP, THEME_TOGGLE_BUTTON, _asset_url, render_app_nav


def render_queue(base_url: str, token: str, enabled: bool) -> str:
    base = html.escape(base_url, quote=True)
    setup = "" if enabled else '<p class="queue-notice">Enable the queue with <code>peanut-review serve --queue-config /path/to/queue.json</code>. See the review queue setup in the README.</p>'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>peanut-review — review queue</title>
<link rel="icon" href="{FAVICON_HREF}">
<link rel="stylesheet" href="{html.escape(_asset_url(base_url, "style.css"), quote=True)}">
{THEME_BOOTSTRAP}</head>
<body class="index queue-page">
<header>
<h1><a href="{base}/">🥜 peanut-review</a></h1>
{render_app_nav(base_url, "queue")}
<span class="spacer"></span>{THEME_TOGGLE_BUTTON}
<button id="queue-refresh" type="button">Refresh queue</button>
</header>
<main class="index-main">
<div class="queue-heading"><div><h2>Your review queue</h2><p class="meta">Review requests and tracked PRs across your accounts.</p></div><span id="queue-count" class="meta" aria-live="polite"></span></div>
<p class="meta">Copy a review task into your driver conversation to prepare the checkout and run the review.</p>
{setup}
<div id="queue-accounts" class="queue-accounts"></div>
<div class="queue-toolbar">
<input id="queue-search" type="search" placeholder="Search PRs, repositories, authors…" aria-label="Search review queue">
<select id="queue-account" aria-label="Filter by account"><option value="">All accounts</option></select>
<details id="queue-repos" class="queue-picker"><summary id="queue-repos-summary">All repositories</summary>
<div class="queue-picker-panel"><div id="queue-repos-list" class="queue-picker-list"></div><button id="queue-repos-clear" type="button">Show all repositories</button></div></details>
<details id="queue-labels" class="queue-picker"><summary id="queue-labels-summary">All labels</summary>
<div class="queue-picker-panel"><div id="queue-labels-list" class="queue-picker-list"></div><button id="queue-labels-clear" type="button">Show all labels</button></div></details>
<select id="queue-request-type" aria-label="Filter by request type">
<option value="" selected>All request types</option><option value="team">Team request</option><option value="direct">Direct request</option>
</select>
<select id="queue-filter" aria-label="Filter review queue">
<option value="attention">Needs attention</option><option value="open" selected>All open PRs</option><option value="requested">Review requested</option><option value="stale">Stale reviews</option><option value="running">Running reviews</option><option value="all">Include closed PRs</option>
</select>
<label class="queue-toggle"><input id="queue-hide-approved" type="checkbox"> Hide PRs I approved</label>
<label class="queue-toggle" title="Hides PRs whose latest commit you reviewed on GitHub"><input id="queue-hide-reviewed" type="checkbox"> Hide PRs I reviewed</label>
<label class="queue-toggle"><input id="queue-hide-drafts" type="checkbox"> Hide drafts</label>
<label class="queue-toggle"><input id="queue-hide-stale" type="checkbox"> Hide stale</label>
<label class="queue-toggle"><input id="queue-hide-mine" type="checkbox" checked> Hide my PRs</label></div>
<p id="queue-error" class="queue-notice error" role="alert" hidden></p>
<p id="queue-copy-status" class="meta" role="status"></p>
<div class="queue-table-wrap"><table class="sessions queue-table">
<thead><tr><th>Pull request</th><th>Account / request</th><th>Review</th><th>Freshness</th><th>Actions</th></tr></thead>
<tbody id="queue-rows"></tbody></table></div>
<div id="queue-empty" class="empty">{"Loading your review queue…" if enabled else "Queue is not configured."}</div>
</main>
<dialog id="queue-agents-dialog" aria-labelledby="queue-agents-title">
<h2 id="queue-agents-title">Agents for this review</h2>
<p class="sub">Turn each persona on or off and choose its model. Copying puts that lineup in this task only. Settings stay unchanged.</p>
<div id="queue-agents-body"></div>
<p id="queue-agents-status" class="settings-status" role="status"></p>
<div class="delete-actions">
<button id="queue-agents-cancel" type="button">Cancel</button>
<button id="queue-agents-copy" type="button">Copy review task</button>
</div>
<datalist id="queue-model-options"></datalist>
</dialog>
<dialog id="queue-task-dialog" aria-labelledby="queue-task-title">
<h2 id="queue-task-title">Copy task to your driver</h2>
<p>Automatic copying is unavailable. Copy the selected text and paste it into your driver conversation.</p>
<textarea id="queue-task-text" aria-label="Review task" rows="14" readonly></textarea>
<button id="queue-task-close" type="button">Close</button>
</dialog>
<script>window.PR_BASE_URL = {json.dumps(base_url).replace('<', chr(92) + 'u003c')}; window.PR_QUEUE_TOKEN = {json.dumps(token)}; window.PR_QUEUE_ENABLED = {json.dumps(enabled)};</script>
<script src="{html.escape(_asset_url(base_url, 'queue.js'), quote=True)}"></script>
</body></html>'''
