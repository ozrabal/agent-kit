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
`hooks.stop_max_blocks` (Stop blocks in a row before giving up), `hooks.file_gate_timeout`,
`hooks.note_chars`, `hooks.error_window_hours`. Every hook call is logged in
`.agent/runs/hooks.jsonl`; `agent-kit log <issue>` shows them, `agent-kit report` counts them.

Rules for specific paths live in `.knowledge/rules/*.md` (frontmatter `paths: [globs]`, `summary:`).
`agent-kit kb context` includes the full rules for the files it picks; at session start you get a
one-line digest. During a loop run a guard (`guard.protected`, `guard.deny_bash`, `guard.always`)
blocks edits outside the worktree or on protected paths and risky Bash (push, reset --hard,
curl | sh, gh pr merge). A block is not an error to work around: do what the message allows.

## After finishing work outside the loop

- `agent-kit kb update` (incremental; only changed files are re-parsed, LLM summaries only for
  new files without a header comment). Commit `.knowledge/` with the change.
- Add a reusable pitfall to `.knowledge/gotchas.md` or a project rule to `conventions.md`, one bullet.

## Ticket loop

- `agent-kit doctor`: checks tools, auth, config, labels, base branch, gates and knowledge base, with a
  fix hint for each failure. Run it first when a run fails for setup reasons.
- `agent-kit status`: ready, blocked and not-ready (refine) tickets, running PRs, their cost and lock holder.
- `agent-kit report [--json] [--since <ts>]`: cost, tokens, fix and review rounds, escalated rounds, red/green
  result, review decisions (skipped / light / full) and hook blocks per ticket run. With `--since`, the
  counts come only from calls and state entries at or after it, not the run's whole history.
- With `loop.tdd.enabled`, the tests you add must fail on the base branch: they are run there with the
  old code, and tests that pass without your change (or no tests) come back as a review finding.
- `agent-kit refine <issue>`: is the ticket ready (an "Acceptance criteria" section with items, not too
  long, not too many criteria)? If not, one lean LLM call posts a proposal (criteria, a split, questions) as an
  issue comment and labels it `needs-refinement`. `refine --apply <issue>` applies the posted proposal (edit its
  JSON block first if needed): criteria into the body, or split issues created with "Blocked by" and the
  parent closed. Nothing gets the ready label back automatically. Before each ticket the loop runs the same
  check (`loop.refine`: `check` skips not-ready tickets, `auto` also proposes, `off`).
- `agent-kit run [--ticket N] [--wait] [--max N] [--dry-run]`: dependency aware; pauses for the human to
  merge each PR (with `merge.policy: "auto"`, `--wait` polls merge a green PR itself). With `loop.questions.enabled`,
  the implementer may stop and ask: questions go to the issue (label `needs-human`), and a reply there
  resumes the session on the next `run`. With `loop.parallel` > 1, independent ready tickets run at the same time, each in its own
  worktree and process (output prefixed `[agent-kit #N]`); `.agent/runs/<issue>/lock` keeps two runs off
  one ticket (a crashed run's lock is replaced). `--wait` polls open PRs: no LLM cost unless there are
  new review comments (lean triage, then a fix if asked) or failed CI on a new head SHA.
- `agent-kit address <issue>`: handle new review comments and failed CI on that ticket's open PR now.
  Reply drafts land in `.agent/runs/<issue>/replies.md` (posted only with `pr.post_replies`). When the
  limits are reached the run becomes `needs_human` (label + PR comment); `address` again overrides it.
- `agent-kit retro [--since <ts>] [--no-llm]`: gate errors, review findings and denied tool calls that
  repeat across runs, and the most expensive steps. Without `--no-llm`, one lean call proposes rules
  (`.knowledge/rules/retro-*.md`), gotchas and config changes; they land in the working tree, uncommitted,
  for a human to review. `doctor` warns when something repeats since the last retro.
- Run state and cost logs: `.agent/runs/<issue>/` (`state.json`, `cost.jsonl`, `pr.md`, `context.md`,
  and one `<step>.log` per Claude session). `agent-kit log <issue>` prints what Claude did, readably.
- A failed run leaves a draft PR with a "Needs a human" section; rerunning `--ticket N` resumes it.
- Config: `.agent/config.json` (gates, labels, models, budgets, PR sections). `agent-kit init` drafts it.
