# agent-kit

A stack-independent ticket loop and project knowledge base for coding agents.
Python 3 standard library only, plus `git`, `gh` and the `claude` CLI.

Rule of the design: **scripts do everything that does not need judgment.** The LLM is
called only to implement, fix, review and write one-line file summaries.

## Install

```sh
git clone <this repo> && cd agent-kit && ./install.sh
```

This links `agent-kit` into `~/.local/bin` and the Claude skill into `~/.claude/skills/agent-kit`,
which tells every Claude session how to use the knowledge base. Edits in the clone take effect
immediately.

## Develop

```sh
python3 -m unittest discover -s tests
```

## Per project

```sh
agent-kit init [--claude-md]   # detect stack -> .agent/config.json, seed .knowledge/, .gitignore
agent-kit kb update            # build the knowledge base (first run: ~1 min, cents)
agent-kit status               # ready / blocked tickets
agent-kit run --dry-run        # show the prompt for the next ticket, change nothing
agent-kit run                  # do the next unblocked ticket, open a PR, pause for merge
agent-kit run --wait           # keep going: poll for merges (no LLM), then the next ticket
```

Commit `.agent/config.json` and `.knowledge/`. `.agent/runs/` and `.knowledge/index.sqlite`
are local.

## One ticket

| Step | Done by |
|---|---|
| reconcile merged PRs: label `done`, close issue, remove worktree | script |
| pick the lowest ready issue whose "Blocked by" issues are all done | script |
| label `in-progress`, worktree from `origin/<base>`, setup commands | script |
| context pack from `.knowledge/` (FTS5 search + import graph + past tickets + gotchas) | script |
| implement | LLM (full Claude Code, scoped tools and Bash allowlist) |
| autofix (formatters), then gates; failures compacted to the relevant lines | script |
| fix gate failures, up to `loop.max_fix_rounds` | LLM, same session resumed |
| commit | script |
| review: spec criteria + findings, diff only, lean system prompt, Read tools | LLM |
| fix blocking findings, up to `loop.max_review_rounds` | LLM, same session resumed |
| ticket note, lessons into gotchas/conventions, kb update, commit | script (+ LLM summaries for new files) |
| push, PR with `Closes #N`, gates, review result, concerns | script |

If gates or review still fail after the rounds, the loop pushes a **draft** PR with a
"Needs a human" section and stops. `agent-kit run --ticket N` resumes it.

## Token use

- Agents get a prepared context pack (~2-4k tokens) instead of exploring.
- Review and summaries run with a replaced system prompt and no MCP: ~1k tokens of overhead
  instead of ~50k.
- Gate output is compacted to error lines + tail.
- Fix rounds resume the implementer's session (cached context) instead of starting over.
- Summaries come from header comments when present; the LLM only fills gaps, and only re-runs
  when a file's symbols or size change a lot.
- Every call is logged with cost: `.agent/runs/<n>/cost.jsonl`; `agent-kit status` shows totals.

## Config (`.agent/config.json`)

Kit defaults live in `agentkit/config.py`; the project file overrides any key. Most useful:
`gates`, `autofix`, `workspace.setup`, `workspace.env`, `tracker.labels`, `merge.max_open_prs`,
`models`, `budget_usd`, `loop.*`, `pr.sections`, `implement.allow_bash`, `kb.exclude`.

## Extending

- Another tracker: add a class with the `GitHub` interface in `agentkit/tracker.py`.
- Another language's symbols/imports: add regexes to `SYM` / `imports()` in `agentkit/kb.py`.
- Prompts: `prompts/*.md` (`$placeholders`).
