#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: copilot-task [OPTIONS] --workspace DIR [PROMPT...]

Run a GitHub Copilot CLI agent non-interactively (`copilot -p`) to produce a
markdown file. The agent's stdout is captured into output.md.

The agent may run shell commands, which it needs for the peanut-review CLI,
builds, and tests. File-editing tools, `git push`, and skills are disabled.

Options:
  --model MODEL          Copilot model id (e.g. gpt-6-astra). Defaults to
                         Copilot's configured default if omitted.
  --reasoning-effort EFFORT
                         Reasoning effort for this run; supported levels
                         depend on the model.
  --workspace DIR        Workspace directory (required)
  --output-dir DIR       Output directory (default: <workspace>/.copilot/tasks)
  --name NAME            Task name for the output subdirectory (default: timestamp)
  --timeout SECS         Timeout in seconds (default: 480)
  --prompt TEXT|FILE     Prompt text, or path to a prompt file (single token + exists)
  --prompt-file FILE     Read prompt from FILE
  --dry-run              Print the command without executing
  -h, --help             Show this help

The prompt can be provided as: --prompt, --prompt-file, or trailing positional
arguments. If multiple sources are given, the first one wins (in that order).
EOF
    exit "${1:-0}"
}

model=""
reasoning_effort=""
workspace=""
output_dir=""
task_name=""
timeout_secs=480
opt_prompt=""
opt_prompt_file=""
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       model="$2"; shift 2 ;;
        --reasoning-effort) reasoning_effort="$2"; shift 2 ;;
        --workspace)   workspace="$2"; shift 2 ;;
        --output-dir)  output_dir="$2"; shift 2 ;;
        --name)        task_name="$2"; shift 2 ;;
        --timeout)     timeout_secs="$2"; shift 2 ;;
        --prompt)      opt_prompt="$2"; shift 2 ;;
        --prompt-file) opt_prompt_file="$2"; shift 2 ;;
        --dry-run)     dry_run=1; shift ;;
        -h|--help)     usage 0 ;;
        --)            shift; break ;;
        -*)            echo "Error: Unknown option: $1" >&2; usage 1 ;;
        *)             break ;;
    esac
done

positional_prompt="$*"

resolve_prompt_arg() {
    local text="$1"
    if [[ "$text" != *" "* && -f "$text" ]]; then
        cat "$text"
    else
        printf '%s' "$text"
    fi
}

if [[ -n "$opt_prompt" ]]; then
    prompt="$(resolve_prompt_arg "$opt_prompt")"
elif [[ -n "$opt_prompt_file" ]]; then
    if [[ ! -f "$opt_prompt_file" ]]; then
        echo "Error: --prompt-file not found: $opt_prompt_file" >&2
        exit 1
    fi
    prompt="$(cat "$opt_prompt_file")"
elif [[ -n "$positional_prompt" ]]; then
    prompt="$positional_prompt"
else
    prompt=""
fi

if [[ -z "$workspace" || -z "$prompt" ]]; then
    echo "Error: --workspace and a prompt are required." >&2
    usage 1
fi

workspace="$(realpath "$workspace")"
if [[ ! -d "$workspace" ]]; then
    echo "Error: workspace does not exist: $workspace" >&2
    exit 1
fi

COPILOT="${COPILOT:-$(command -v copilot || true)}"
if (( ! dry_run )) && [[ ! -x "$COPILOT" ]]; then
    echo "Error: copilot not found (set COPILOT env var to override)." >&2
    exit 1
fi

if [[ -z "$output_dir" ]]; then
    output_dir="$workspace/.copilot/tasks"
fi
if [[ -z "$task_name" ]]; then
    task_name="$(date +%Y%m%d-%H%M%S)"
fi
task_dir="$output_dir/$task_name"
output_file="$task_dir/output.md"
mkdir -p "$task_dir"

meta_file="$task_dir/meta.json"
start_time="$(date -Iseconds)"
pgid="$(ps -o pgid= -p "$$" | tr -d ' ' || true)"

if command -v jq >/dev/null; then
    jq -n \
        --arg model "$model" \
        --arg reasoning_effort "$reasoning_effort" \
        --arg workspace "$workspace" \
        --arg prompt "$prompt" \
        --arg start "$start_time" \
        --arg timeout "$timeout_secs" \
        --arg pid "$$" \
        --arg pgid "$pgid" \
        --arg supervisor_pid "${PEANUT_SUPERVISOR_PID:-}" \
        '{runner: "copilot", model: $model, reasoning_effort: $reasoning_effort,
          workspace: $workspace, prompt: $prompt,
          start: $start, timeout: ($timeout | tonumber),
          pid: ($pid | tonumber),
          pgid: (if $pgid == "" then null else ($pgid | tonumber) end),
          supervisor_pid: (if $supervisor_pid == "" then null else ($supervisor_pid | tonumber) end)}' \
        > "$meta_file"
fi

echo "copilot-task" >&2
echo "  Runner:    copilot (copilot -p)" >&2
echo "  Model:     ${model:-<copilot default>}" >&2
echo "  Reasoning: ${reasoning_effort:-<copilot default>}" >&2
echo "  Workspace: $workspace" >&2
echo "  Output:    $output_file" >&2
echo "  Timeout:   ${timeout_secs}s" >&2
echo "" >&2

# Prompt mode cannot ask for approval, so every permission is decided here.
# Shell access stays open because the session directory, build trees, and the
# peanut-review CLI live outside the workspace. Skills are hidden so a
# reviewer cannot load the user's personal skills.
cmd=("${COPILOT:-copilot}"
     -C "$workspace"
     --silent
     --no-color
     --no-ask-user
     --disable-builtin-mcps
     --allow-all-tools
     --allow-all-paths
     --allow-url=github.com
     --allow-url=api.github.com
     --allow-url=gist.github.com
     --deny-tool=write
     "--deny-tool=shell(git push)"
     --excluded-tools=skill)

if [[ -n "$model" ]]; then
    cmd+=(--model "$model")
fi
if [[ -n "$reasoning_effort" ]]; then
    cmd+=(--reasoning-effort "$reasoning_effort")
fi

cmd+=(-p "$prompt")

if (( dry_run )); then
    printf '%q ' "${cmd[@]}" >&2
    echo >&2
    exit 0
fi

exec "${cmd[@]}" > "$output_file" < /dev/null
