---
name: agent-kit
description: Use in any repo that has a .knowledge/ folder or .agent/config.json, before exploring code for a task, before running checks, or when asked to run, resume or inspect the ticket loop. Gives a cheap context pack from the project knowledge base, compact quality gates, and the scripted ticket loop.
---

# agent-kit

A local CLI (`agent-kit`; its source repo is where `readlink -f "$(which agent-kit)"` points) that keeps a project knowledge base and runs a
ticket loop where scripts do the mechanical work and the LLM only implements and reviews.

## Before exploring code

```sh
agent-kit kb context "<task words or identifiers>"   # or: agent-kit kb context '#<issue>'
```

It prints the most relevant files with one-line summaries and symbols, their import-graph
neighbours, related past tickets, gotchas and conventions, in a few thousand tokens. Start from
those files; search further only when they are not enough. `.knowledge/tree.md` is the full map.

## Checking work

`agent-kit gates` runs the project's configured lint/type/test commands and prints only the
failing lines. `agent-kit gates <name>` runs one; `--fix` runs formatters first.

With hooks installed (`agent-kit hooks install`), file gates run after each edit and the gates
run before you finish; a failure comes back as hook feedback. Fix it, don't work around it.
Config: `file_gates` (`{name, match, cmd with {file}, timeout}`), `hooks.stop_gate`,
`hooks.stop_max_blocks` (Stop blocks in a row before giving up). Every hook call is logged in
`.agent/runs/hooks.jsonl`; `agent-kit log <issue>` shows them, `agent-kit report` counts them.

## After finishing work outside the loop

- `agent-kit kb update` (incremental; only changed files are re-parsed, LLM summaries only for
  new files without a header comment). Commit `.knowledge/` with the change.
- Add a reusable pitfall to `.knowledge/gotchas.md` or a project rule to `conventions.md`, one bullet.

## Ticket loop

- `agent-kit doctor`: checks tools, auth, config, labels, base branch, gates and knowledge base, with a
  fix hint for each failure. Run it first when a run fails for setup reasons.
- `agent-kit status`: ready and blocked tickets, running PRs and their cost.
- `agent-kit report [--json] [--since <ts>]`: cost, tokens, fix and review rounds and hook blocks
  per ticket run.
- `agent-kit run [--ticket N] [--wait] [--max N] [--dry-run]`: one ticket at a time, dependency
  aware; pauses for the human to merge each PR. `--wait` polls for the merge without LLM cost.
- Run state and cost logs: `.agent/runs/<issue>/` (`state.json`, `cost.jsonl`, `pr.md`, `context.md`,
  and one `<step>.log` per Claude session). `agent-kit log <issue>` prints what Claude did, readably.
- A failed run leaves a draft PR with a "Needs a human" section; rerunning `--ticket N` resumes it.
- Config: `.agent/config.json` (gates, labels, models, budgets, PR sections). `agent-kit init` drafts it.
