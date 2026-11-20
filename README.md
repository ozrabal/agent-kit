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
agent-kit doctor [--no-gates]  # check tools, gh auth, config keys, labels, base branch, gates, KB
agent-kit status               # ready / blocked tickets
agent-kit hooks install [--user] # Claude Code hooks: file gates after each edit, gates before stopping
agent-kit report [--json]      # cost, tokens, fix and review rounds per ticket run
agent-kit log 12               # what Claude did on ticket #12: tool calls, errors, denials, hook calls
agent-kit run --dry-run        # show the prompt for the next ticket, change nothing
agent-kit run                  # do the next unblocked ticket, open a PR, pause for merge
agent-kit run --wait           # keep going: poll for merges (no LLM), then the next ticket
```

Commit `.agent/config.json`, `.knowledge/` and `.claude/settings.json` (the loop's worktrees only
see committed hooks). `.agent/runs/` and `.knowledge/index.sqlite`
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
| fix gate failures, up to `loop.max_fix_rounds` (the last one on `models.escalate`, if set) | LLM, same session resumed |
| commit | script |
| red/green (`loop.tdd`, opt-in): the branch's new tests run on a base worktree, must fail there | script |
| review plan: skip (docs only, or small and no acceptance criteria), light or full model | script |
| review: spec criteria + findings, diff only, lean system prompt, Read tools | LLM (unless skipped) |
| fix blocking findings (and red/green problems), up to `loop.max_review_rounds` (last one escalated) | LLM, same session resumed |
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
- Every call is logged with cost: `.agent/runs/<n>/cost.jsonl`; `agent-kit status` shows totals and
  `agent-kit report` breaks them down per ticket (tokens, fix and review rounds).
- `python3 sandbox/bench.py` runs fixed sandbox tickets and saves that report; `--compare a.json b.json`
  shows what a change did to cost, tokens and rounds.
- Hooks catch lint and gate failures inside the implement session, so fewer resumed fix rounds
  with a new prompt are needed.
- Path-scoped rules (`.knowledge/rules/*.md`, frontmatter `paths: ["src/api/**"]` and `summary:`;
  no `paths` = everywhere; globs use `fnmatch` on repo-relative paths, so `*` also crosses folders) replace pasting everything everywhere: the context pack has the full
  text of the rules for the files it picked, the review prompt has `conventions.md` plus the rules
  for the changed files, and other sessions get a one-line-per-rule digest
  (`.knowledge/rules-digest.md`, written by `kb update`).
- The script decides whether and with which model the review runs (`review_plan`): no review when
  every changed file matches `review.skip_paths`, or when the diff has at most
  `review.skip_under_lines` changed lines and the ticket has no acceptance criteria (an
  "Acceptance criteria" heading or a `- [ ]` checklist); `models.review` for `review.risky_paths` or
  more than `review.light_max_lines` lines, `models.review_light` otherwise. The decision is logged
  (`review plan: ...`), stored in `state.json` (`plan` on each `reviews` entry), listed by `agent-kit log <n>` under
  `## review plan`, counted by `agent-kit report` (`rv_skip`, `rv_light`, `rv_full`, plus review
  cost `rv_usd` and `rv_cache_r`) and stated in the PR ("Automated review skipped: <reason>").
- Prompts put stable text first: the review's instructions are its system prompt
  (`prompts/review-system.md`, the same for every ticket), so repeated reviews can read it from the
  prompt cache; the user message holds only conventions/rules, ticket, diff stat and diff. The
  implement prompt starts with how to work and what to return; ticket and context pack come last.
- Implement, fix and review stream their sessions to `.agent/runs/<n>/<step>.log` and print each
  tool call while they run. Same tokens as before; only the output format changes.

## Config (`.agent/config.json`)

Kit defaults live in `agentkit/config.py`; the project file overrides any key. Most useful:
`gates`, `autofix`, `workspace.setup`, `workspace.env`, `tracker.labels`, `merge.max_open_prs`,
`models`, `budget_usd`, `loop.*`, `pr.sections`, `implement.allow_bash`, `kb.exclude`, `file_gates`,
`hooks.*`.

Hooks (`agent-kit hooks install`, `uninstall` removes only agent-kit's entries):

- `file_gates`: run after Claude edits a matching file, e.g.
  `{"name": "ruff", "match": "*.py", "cmd": "ruff check {file}", "timeout": 60}`. `{file}` is the
  path relative to the repo. A failure is shown to Claude in the same turn. Default `[]`.
- `hooks.stop_gate` (default `true`): autofix + `gates` run when Claude wants to finish; failures
  send it back to fix them, at most `hooks.stop_max_blocks` (default `2`) times in a row.
  The loop still runs the gates after implement as a safety net; `state.json` records `fix_rounds`.
- `hooks.file_gate_timeout` (default `60` s): default timeout of a file gate without its own.
- Every hook call in a repo with `.agent/` is logged to `.agent/runs/hooks.jsonl` (event, session,
  file, outcome `pass|block|skip|give_up|error`, gates with times, and a note of up to
  `hooks.note_chars` (default `600`) chars). This includes a config that fails to load. The log is
  the only record of what a post-edit hook told Claude: that feedback is not in the session log.
  `agent-kit run` prints each call live during implement and fix sessions (review edits nothing),
  `agent-kit log <n> [step]` lists a ticket's calls under `## hooks` (matched by Claude session id),
  `agent-kit report` counts them per ticket (`hooks`, `edit_blk`, `stop_blk`, `guard_blk`, `hook_err`) and
  `doctor` warns about hook errors in the last `hooks.error_window_hours` (default `24`).
- `guard.*`: a PreToolUse scope guard (`agent-kit hook guard`). During a loop run (a run whose
  worktree is the session's checkout and that is still implementing or fixing) it blocks
  Edit/Write/MultiEdit/NotebookEdit outside the worktree or on `guard.protected` paths (default
  `.git/*`, `.agent/*`, `.github/workflows/*`, `.claude/*`), and Bash commands matching a
  `guard.deny_bash` regex (default: `git push`, `git reset --hard`, `rm -rf /`, `curl ... | sh`,
  `gh pr merge`, `rm -rf ~`, redirects, `tee`, `cp`, `mv` into `~/`). The block message says what is allowed instead. Outside a
  run it does nothing unless `guard.always` is `true` (then the repo is the scope). Denials show in
  `agent-kit log <n>` as `hook guard ...: BLOCK`, in `agent-kit report` as `guard_blk`, and in
  `doctor` (last `hooks.error_window_hours`).
- `session-start` (SessionStart): outside a loop run, prints `.knowledge/rules-digest.md` and a hint
  to use `agent-kit kb context` into Claude's context; in a run it prints nothing (the prompt has
  the pack). `subagent-start` (SubagentStart, which can add context but not block): gives every
  sub-agent the run's `context.md`, or the digest outside a run, as `additionalContext`.
- `review.*` decides the review (see Token use): `skip_paths` (default `["*.md", "docs/**",
  "*.txt"]`), `skip_under_lines` (default `0` = never skip by size), `risky_paths` (default `[]`,
  e.g. `["**/auth/**", "**/migrations/**"]`), `light_max_lines` (default `300`). Models:
  `models.review` (default `opus`) and `models.review_light` (default `sonnet`); both use
  `budget_usd.review`. Globs as for rules: `*` crosses folders, `**/x` also matches `x`.
- `loop.tdd` (opt-in, `enabled: false`): after the gates pass, the test files the branch added or
  changed (matching `test_paths`, default `["tests/**", "**/*_test.*", "**/test_*.*", "**/*.test.*",
  "**/*.spec.*"]`) are copied into a temporary worktree at `origin/<base>` (system temp dir,
  `workspace.setup` runs there first, always removed afterwards) and the test gate runs: `gate` by
  name, or `null` = the first gate whose name contains "test". Failing there is the goal (`red`).
  Passing there (`passes_on_base`), or no test files changed when not every file matches
  `review.skip_paths` (`no_tests`), adds a `major` finding to the review, even a skipped one, so the
  normal review-fix round handles it. Any failure on the base counts as red, including an import
  error of a new helper. Deleted tests are not copied. No gate found or setup failing on the base:
  `skipped` with the reason (`doctor` fails on a gate it cannot resolve). Shown in the run log
  (`tdd: RED ...`), `state.json` (`tdd`, all checks in `tdd_checks`), `agent-kit log <n>` (`## tdd`),
  `agent-kit report` (`tdd`) and the PR's Gates section (`Tests: red on base -> green on branch`).
  No LLM; it costs one extra test run per review round.
- `models.escalate` (default `null`, e.g. `"opus"`): the model for the last gate-fix round
  (`fix_round == loop.max_fix_rounds`) and the last review-fix round (`== loop.max_review_rounds`).
  The session is resumed with `--model`, so it keeps its context; the new model has no cache for it,
  so that round pays the cache write once. Logged (`escalate: ...`), in `state.json` (`escalations`),
  `agent-kit log <n>` (`## escalations`), `agent-kit report` (`escal`) and per call in `cost.jsonl` (`model`).
- Hooks do nothing in repos without `.agent/config.json`. Gates in hooks cost no tokens; Claude
  reads the compacted failure output in its session.

## Extending

- Another tracker: add a class with the `GitHub` interface in `agentkit/tracker.py`.
- Another language's symbols/imports: add regexes to `SYM` / `imports()` in `agentkit/kb.py`.
- Prompts: `prompts/*.md` (`$placeholders`).
