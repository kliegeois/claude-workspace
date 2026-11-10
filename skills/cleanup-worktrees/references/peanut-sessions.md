# Peanut-review session cleanup

Manage whole session directories without launching reviewers, curating comments,
or publishing review state. The workspace tool is
`tools/peanut-review/bin/peanut-review`; inspect its current behavior when needed.

## Discover sessions and runtime evidence

Resolve `reviewRoot` from `.cache/peanut-review/.peanut-review.json` and any
additional roots served by the UI. Inventory directories containing `session.json`
independently of worktrees, including sessions whose checkout is gone. Shared
configuration, `.queue/`, PID files, and parent roots are not sessions.

Join sessions to checkouts using `workspace` plus `repo_relative` and repository
identity. `git_common_dir` identifies durable Git storage; `workspace_branch`
records the branch. Preserve the saved GitHub host/account when checking PR state.

The dashboard can run in another Docker container. Its supervisors already write
start times, heartbeats, terminal process states, end times and exit codes into
shared `log/<agent>/meta.json` files. GitHub review runs also record launch IDs
and start/finish times in `review-completion.json`. Read this evidence locally;
server reachability and foreign PIDs are not prerequisites for cleanup.

## Read-only metadata inspection

On Linux with Bubblewrap available, run the workspace CLI inside a read-only
mount and separate PID/network namespaces, passing every configured review root:

```bash
bwrap --unshare-all --ro-bind / / --dev /dev --tmpfs /proc --die-with-parent \
  --setenv PYTHONDONTWRITEBYTECODE 1 -- \
  "$PR_BIN" --session "$session_path" cleanup-status --root "$review_root"
```

The empty `/proc` hides host process handles; it avoids needing permission to
mount a new proc filesystem inside Docker. Existing files remain read-only,
including the live session store. The checker cannot signal processes outside
its PID namespace. Keep reports outside the sandbox by capturing stdout. If
isolation fails during a live preview, report the failure instead of silently
rerunning without it. Do not restart the server or change live runtime code to
enable an inspection.

The private `/dev` supplies usable null devices for Git subprocesses without
exposing host process handles or making the session store writable.

Repeat `--root` for additional roots. The command includes every session sharing
the checkout. For a registered checkout without a session, use another session
in the same repository as the anchor and add `--worktree "$worktree_path"`.
The response names its worktree, related sessions, per-agent evidence, active
legacy queue jobs, recent activity, errors, and `state: idle|busy|unknown`.
It reads shared files twice without refreshing statuses, creating locks,
contacting a server, fetching Git data, or looking up saved PIDs.

Interpret evidence as follows:

- A recent unfinished launch or heartbeat protects the session as busy. The
  supervisor normally writes heartbeats every 30 seconds; the reader allows
  120 seconds. An old or missing heartbeat on an unfinished run means unknown,
  never automatic abandonment or exit.
- A consistent terminal supervisor state, integer exit code and ordered start/end
  timestamps establish that the tracked reviewer finished. When a launch record
  exists, its latest run must also be finished and covered by that final metadata.
  A newer launch invalidates an older completion. Old sessions with complete
  supervisor records do not require migration or a server restart.
- `round-done`, `result.json`, UI agent status, and saved PIDs alone are not exit
  evidence. Missing or malformed records, contradictory timestamps, and
  unresolved SSH cleanup protect the candidate. Confirmed remote exit in shared
  metadata is sufficient; do not require an SSH connection solely for inspection.
- New prompts, review writes or runtime metadata impose a 120-second quiet period,
  covering preparation and transitions between reviewers and curator. Relevant
  file or session-membership changes during the read invalidate the observation.
  Routine queue polling and activity on another checkout do not protect this one.

An idle result covers tracked review activity only. Combine it with local process
checks, task/progress notes, and local file checks for independently launched
builds, tests, or shells. Retain a candidate when there is specific evidence of
such use or unresolved ownership. Do not require access to every other container
merely because it could hypothetically use a shared path.

Recheck immediately before removal. The result is a point-in-time observation,
not a deletion lease. In preview, do not repair stale metadata, run ordinary
`peanut-review status`, or construct a review queue object: those can write shared
state. Busy or unknown items remain intact; continue with independent idle ones.

## Forwarded ports and status side effects

A port published as `127.0.0.1:27183:27183` is bound to the Docker host's loopback.
Neither another container's `localhost` nor the host's bridge gateway is that
address. A VS Code/SSH forward to a laptop does not automatically make it
reachable from the cleanup container. The `0.0.0.0` address in `web.pid` is a
server bind address, not a usable client destination. Docker CLI availability
also does not imply access to the daemon or another container's processes.

Network access is optional for cleanup. If needed for separate diagnostics,
use an established reachable URL or a host SSH tunnel; do not expose the service
on all host interfaces or restart/reconfigure live containers just to inspect
them. Ordinary session-page GETs and `/api/session` refresh and save statuses,
so HTTP GET alone is not a read-only guarantee. Use the isolated metadata reader
for cleanup evidence even when the dashboard is reachable.

## Preserve Git history, then delete

After eligibility and runtime checks pass, ensure recorded base, topic, original
head, current head, and earlier retained review revisions remain readable in the
surviving repository. During apply only, repair retention with:

```bash
"$PR_BIN" --session "$session_path" retain-git --repo "$canonical_repo"
```

Verify the commits and cumulative `refs/peanut-review/` refs. Keep these refs even
when deleting the session; they preserve code history without retaining session
logs or runtimes. Never guess the ref namespace from a directory name.

A normal cleanup invocation authorizes permanent deletion of eligible session
contents, including review comments/history, reports, prompts, signals, logs,
scratch files and bundled runtimes. Do not archive these by default, ask for a
second deletion approval, or substitute `peanut-review archive` (which writes Git
notes). Honor explicit keep instructions and separate user work stored there;
retain a session if its contents cannot be classified as disposable session data.

Before deletion, recheck runtime evidence and source identity. Remove only the
exact inventoried directory below a configured session root. Reject symlink
substitutions, nested mounts or repositories; do not follow symlinks or delete
external targets. Do not delete shared queue state or configuration. Verify that
the directory is absent and other sessions remain discoverable. Record deletion
and retained Git refs in the run manifest. Deleted session contents have no
restore procedure; a removed worktree can be recreated from its preserved branch.

## Optional archives

Only when explicitly requested, archive an eligible idle session under the
cleanup run directory before deleting it. Preserve all entries and symlinks
without traversing external targets. Verify archive entries, contents and
checksums against the source, then recheck activity and source stability before
deletion. Retain the source on a failed check. Record archive location, checksum
and extraction instructions that refuse to overwrite an existing directory.
Subtract archived bytes from reclaimed space. Existing recovery archives are not
automatically purged by changing this policy or running an ordinary cleanup.
