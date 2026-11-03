#!/usr/bin/env bash
# Link the CLI onto PATH and the Claude skill into ~/.claude/skills. Safe to rerun.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${AGENT_KIT_BIN:-$HOME/.local/bin}"
skill_dir="$HOME/.claude/skills"

for tool in python3 git gh claude; do
  command -v "$tool" >/dev/null || echo "warning: $tool not found on PATH" >&2
done

mkdir -p "$bin_dir" "$skill_dir"
ln -sfn "$here/bin/agent-kit" "$bin_dir/agent-kit"
ln -sfn "$here/skills/agent-kit" "$skill_dir/agent-kit"
echo "linked $bin_dir/agent-kit and $skill_dir/agent-kit"
case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "note: add $bin_dir to PATH" >&2 ;; esac
