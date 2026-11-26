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
agent-kit refine 12            # ready? (criteria, size) if not: LLM proposal as a comment, label needs-refinement
agent-kit refine --apply 12    # apply the posted proposal: criteria into the body, or create the split issues
agent-kit run --dry-run        # show the prompt for the next ticket, change nothing
agent-kit run                  # do the next unblocked ticket, open a PR, pause for merge
agent-kit run --wait           # keep going: poll open PRs (comments, CI, merges), then the next ticket
agent-kit address 12           # handle new review comments and failed CI on #12's open PR now
agent-kit retro [--no-llm]     # repeated gate errors, findings, denials across runs -> proposed rules, gotchas
```

Commit `.agent/config.json`, `.knowledge/` and `.claude/settings.json` (the loop's worktrees only
see committed hooks). `.agent/runs/` and `.knowledge/index.sqlite`
are local.

## One ticket

| Step | Done by |
|---|---|
| reconcile merged PRs: label `done`, close issue, remove worktree | script |
| pick the lowest ready issue whose "Blocked by" issues are all done | script |
| refine check: "Acceptance criteria" items, size; a ticket that fails is skipped (`loop.refine`) | script |
| refine proposal for a skipped ticket (`loop.refine: "auto"`): comment + label `needs-refinement` | LLM (lean, once per body) |
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

## Refine and parallel tickets

Before implement, `loop.refine` (default `"check"`) runs a script check: the body has a
`refine.criteria_heading` section (default "acceptance criteria", any heading level, case-insensitive)
with at least one bullet, numbered or checkbox item, the body is at most `refine.max_chars` (default
`8000`, `0` = no limit) and there are at most `refine.max_criteria` (default `8`) criteria. A ticket that
fails is skipped (`check`: log and state only, nothing written to GitHub); with `"auto"`, or by hand with
`agent-kit refine <n>`, one lean LLM call (`models.refine`, default `sonnet`, `budget_usd.refine` 0.5, no
tools, the ticket and its `kb context` pack) proposes `criteria`, a `split` (title, body, criteria,
`blocked_by` = 1-based numbers of earlier parts) and `questions`. The proposal is posted as an issue comment
(readable, plus a JSON block that carries a hash of the body) and the label goes `ready` ->
`tracker.labels.refine` (default `needs-refinement`). Only one proposal per body version: a rerun finds it
in `state.json` or in the kit's comment (trusted authors only). `agent-kit refine --apply <n>` reads that
block (a human may edit it): a split creates the issues in order (label `needs-refinement`, "Blocked by"
from the plan plus the parent's own blockers) and closes the parent as not planned; otherwise the criteria
are written into the body. A human sets `ready` again. An explicit `run --ticket N` is refined too, unless
its run already exists (resume). `--dry-run` and `next` only check.

`loop.parallel` (default `1`): when > 1, `run` picks up to `min(loop.parallel, merge.max_open_prs - open
PRs)` ready, unblocked tickets (an unblocked ticket never depends on another one) and runs each as
`agent-kit run --ticket N` in its own process and worktree; the console shows their lines prefixed
`[agent-kit #N]`, the parent waits for all of them (Ctrl-C stops them; `run --ticket N` resumes one), then
goes on (`--wait`: polls, the next batch when PR slots free up). The parent first refreshes the kb index
and creates the worktrees (the children would race on both); `git fetch`, `worktree add` and pushes take
turns through `.agent/runs/git.lock`. A ticket run, and every `address` pass on its PR (also from `--wait`),
holds `.agent/runs/<n>/lock` (pid and a heartbeat time, rewritten every 60 s); a second run on the ticket is
skipped (a follow-up poll records `skipped: locked by pid X`). A lock whose process is gone, or with no
heartbeat for 10 minutes (the pid now belongs to another program), is stale and replaced. A ticket whose
worktree cannot be prepared is left out of the batch (logged, `error` in its `parallel` state); the others
still run. Needs `workspace.mode: "worktree"`.

Shown in the run log (`refine: ...`, `parallel: #a, #b in 2 slot(s); left out: ...`, lock lines),
`state.json` (`refine`, `refine_checks`, `refine_apply`, `parallel`, `locks`), `agent-kit status`
(`REFINE` lines, `locked by pid`, `batch #a, #b`), `agent-kit log <n>` (`## refine`, `## parallel`, `## locks`),
`agent-kit report` (`rf_chk`, `rf_fail`, `rf_prop`, `rf_usd`, `rf_appl`, `rf_split`, `par` = ran beside
other tickets, `lk_stale`, `lk_busy`) and `doctor` (`refine` mode, `parallel` vs `merge.max_open_prs` and the workspace mode, the label).

## After the PR is open

`agent-kit run --wait` polls every open PR once per `merge.poll_seconds`; `agent-kit address <n>`
does one pass now (also when `merge.max_open_prs` is reached).

| Step | Done by |
|---|---|
| read review comments (inline, review bodies, conversation) and checks on the head SHA | script (`gh`) |
| keep comments that are new: by a trusted author (`pr.trusted_associations`), not handled, thread not resolved, not a `[bot]`, not the kit's own (`<!-- agent-kit -->` marker) | script |
| triage new comments: `fix`, `reply` or `ignore` per comment, from its text and diff hunk | LLM (lean, `models.triage`) |
| reply drafts to `.agent/runs/<n>/replies.md`; posted only with `pr.post_replies` | script |
| fix comments, up to `loop.max_comment_rounds` per PR (last one escalated) | LLM, session resumed (fresh session if the resume fails) |
| failed CI on a new head SHA, once nothing is pending: compacted `--log-failed` output into a fix, up to `loop.max_ci_rounds` | LLM, session resumed |
| gates and gate-fix rounds, commit (`address review` / `fix CI`), push | script (+ LLM for gate fixes) |
| limits reached, gates still failing (the edits stay uncommitted in the worktree) or a fix that changed nothing: label `needs-human`, PR comment, status `needs_human`; polls skip it | script |

Handled comment ids (`handled_comments`) and CI SHAs (`handled_ci`) live in `state.json`, so a restart
never handles one twice. `agent-kit address <n>` on a `needs_human` run is your override: it resets
the round limits and lets the last CI failure be retried. A fix interrupted mid-way (status
`fixing-review-comments` / `fixing-review-ci`) is picked up by the next poll. Every poll decision, triage result and fix is in `state.json`
(`polls`, `triage`, `follow_ups`, `needs_human`), the run log, `agent-kit log <n>` (`## follow-up`),
`agent-kit status` and `agent-kit report` (`polls`, `triaged`, `tr_usd`, `cm_fix`, `ci_fix`, `human`).

## Retro: learn from past runs

`agent-kit retro [--since <ts>] [--no-llm]` reads the runs in `.agent/runs/` (the same runs as
`agent-kit report --since`: a run counts when it has an LLM call at or after the timestamp) and prints:

- totals: cost, fix and review rounds, escalations, failed red/green checks, hook blocks, PR follow-up;
- the most expensive LLM steps;
- gate errors and review findings seen in at least `retro.min_runs` (default 2) different runs. Gate
  errors are the first `retro.error_lines` (default 3) error lines of each failing gate: every failing
  round the loop recorded (`gate_failures` in `state.json`; older runs: the final `gates`), a crashed
  run's error, the post-edit and stop hook blocks, and the failures of `agent-kit gates` calls Claude
  made itself during the run (`gates` rows in the hook log, so this works without the hooks). Lines are normalized before they are compared
  (worktree and repo folders stripped, temp dirs, hex ids and numbers replaced; quoted values kept,
  an apostrophe is not a quote).
  Findings (TDD ones included) are grouped by their normalized text;
- the files and folders findings point at, and denied tool calls: permission denials from the session
  logs, every session in a log (a Bash one is a candidate for `implement.allow_bash`), one without
  details for each further denial `cost.jsonl` counts (lean calls have no log), and scope guard blocks.

Then, unless `--no-llm` or nothing repeats, one lean LLM call (`models.retro`, default `sonnet`,
`budget_usd.retro` 0.5, no tools) gets that report (at most `retro.max_chars`, default 12000) and the
rules and gotchas the project already has (at most `retro.known_chars`, default 3000), and proposes rules, gotchas and config
changes. Retro refuses before the call when tracked files under `.knowledge/` have uncommitted changes,
so the proposal is the only diff. The proposal is never committed:

- rules go to `.knowledge/rules/retro-<summary>.md` (the rules format, so `kb update` and `kb context`
  pick them up; the digest is refreshed). A rule whose file exists, or that matches an existing rule's
  summary and paths, is skipped; nothing is overwritten;
- gotchas are appended to `gotchas.md` (`(retro)`), with the near-duplicate check lessons use;
- config suggestions are only printed, with unknown keys marked;
- `git diff --stat` and the untracked files retro wrote are printed: review, then commit.

Every retro appends a row to `.agent/runs/retro.jsonl` (mode `no-llm`, `nothing`, `refused`, `error` or
`llm`; runs, repeated errors/findings/denials, cost, rules written and skipped, gotchas added, config
suggestions) and logs one line. Its LLM call is logged in `.agent/runs/retro-cost.jsonl`.
`agent-kit report` ends with a `retro:` line (count, cost, proposals, last run), and `doctor` warns
(`retro`) when something repeats in the runs since the last retro that asked the LLM.

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
  no `paths` = everywhere; globs use `fnmatch` on repo-relative paths, so `*` also crosses folders, and a
  `**/` matches no folder too: `src/**/*.py` matches `src/a.py`) replace pasting everything everywhere: the context pack has the full
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
- PR follow-up: a poll with nothing new makes no LLM call. New comments cost one lean triage call
  (`models.triage`, default haiku, `budget_usd.triage`): fixed instructions in its system prompt
  (`prompts/triage-system.md`), each comment with only its diff hunk, no tools. Only `fix` comments and
  failed CI resume the implementer session (`budget_usd.fix`); CI logs are compacted like gate output.
- Refine: a ticket that passes the script check costs nothing. One that fails costs one lean call
  (no tools, fixed instructions in the system prompt `prompts/refine-system.md`, ticket + context pack in
  the user message) only with `loop.refine: "auto"` or `agent-kit refine <n>`, and only once per body version.
- `loop.parallel` adds no LLM calls; it runs the same calls for several tickets at the same time.
- Retro: `--no-llm` and runs where nothing repeats cost nothing. Otherwise one lean call with no tools:
  fixed instructions in the system prompt (`prompts/retro-system.md`), the script's aggregate (capped at
  `retro.max_chars`) and the known rules/gotchas (capped) in the user message; never full logs.
- Implement, fix and review stream their sessions to `.agent/runs/<n>/<step>.log` and print each
  tool call while they run. Same tokens as before; only the output format changes.

## Config (`.agent/config.json`)

Kit defaults live in `agentkit/config.py`; the project file overrides any key. Most useful:
`gates`, `autofix`, `workspace.setup`, `workspace.env`, `tracker.labels`, `merge.max_open_prs`,
`models`, `budget_usd`, `loop.*` (incl. `loop.refine`, `loop.parallel`), `refine.*`, `retro.*`, `pr.sections`, `implement.allow_bash`, `kb.exclude`, `file_gates`,
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
  `agent-kit gates` run inside a loop run's worktree (Claude checking its own work) adds a `gates` row
  there too (`block` with the failures as note, else `pass`), tied to the run by Claude's session id
  (`CLAUDE_CODE_SESSION_ID`), or by the run key when there is none; outside a run it logs nothing.
  `agent-kit run` prints each call live during implement and fix sessions (review edits nothing),
  `agent-kit log <n> [step]` lists a ticket's calls under `## hooks` (matched by Claude session id),
  `agent-kit report` counts them per ticket (`hooks`: all rows incl. `gates`, `edit_blk`, `stop_blk`, `guard_blk`, `gates_blk`, `hook_err`) and
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
  `budget_usd.review`. Globs as for rules: `*` crosses folders, `**/x` also matches `x`, `a/**/b` also matches `a/b`.
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
  The finding blocks even if `major` is not in `review.block_on`. No LLM; it runs after every
  review round (a fix may change the tests), so each round pays one base test run plus
  `workspace.setup` in the temporary worktree.
- `models.escalate` (default `null`, e.g. `"opus"`): the model for the last gate-fix round
  (`fix_round == loop.max_fix_rounds`) and the last review-fix round (`== loop.max_review_rounds`).
  The session is resumed with `--model`, so it keeps its context; the new model has no cache for it,
  so that round pays the cache write once. Logged (`escalate: ...`), in `state.json` (`escalations`),
  `agent-kit log <n>` (`## escalations`), `agent-kit report` (`escal`) and per call in `cost.jsonl` (`model`).
- `loop.max_comment_rounds` / `loop.max_ci_rounds` (default 2 each, per PR): fix rounds after the PR is
  open, for review comments and for failed CI. `models.triage` (default `haiku`) and
  `budget_usd.triage` (default 0.3) for the comment triage. `pr.post_replies` (default `false`):
  post triage reply drafts on the PR; otherwise they are only in `.agent/runs/<n>/replies.md`.
  `pr.trusted_associations` (default `["OWNER", "MEMBER", "COLLABORATOR"]`): only comments by these
  GitHub author associations are triaged, so a stranger's comment on a public repo cannot steer the agent.
  `tracker.labels.needs_human` (default `needs-human`): set when the follow-up gives up (doctor
  checks that the label exists).
- `retro` (for `agent-kit retro`): `min_runs` (default 2, different runs a gate error, finding or
  denial must appear in to count as repeated), `error_lines` (default 3 per failing gate), `top`
  (default 10 items per report section), `max_chars` (default 12000, the report sent to the LLM),
  `known_chars` (default 3000, the known rules and gotchas sent along).
  `models.retro` (default `sonnet`), `effort.retro` (default `null`), `budget_usd.retro` (default 0.5).
- Hooks do nothing in repos without `.agent/config.json`. Gates in hooks cost no tokens; Claude
  reads the compacted failure output in its session.

## Extending

- Another tracker: add a class with the `GitHub` interface in `agentkit/tracker.py`.
- Another language's symbols/imports: add regexes to `SYM` / `imports()` in `agentkit/kb.py`.
- Prompts: `prompts/*.md` (`$placeholders`).
