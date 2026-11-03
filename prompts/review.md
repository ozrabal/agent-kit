Review this change for ticket $key: $title

## Ticket (the spec)
$body

## Project conventions
$conventions

## Gates
All gates passed: $gate_names. Do not report formatting, lint or type issues those gates already cover.

## Diff stat (vs $base)
```
$stat
```

## Diff
```diff
$diff
```

## What to do
You are in the repo checkout of this branch and may Read files for context (tests, callers, surrounding code). Be token-frugal: read only what a finding depends on.

1. For every acceptance criterion in the ticket, decide `met` with one line of evidence (a file, test, or behaviour).
2. Report findings that matter: bugs, missing or weak tests for a criterion, security or data-loss risks, scope creep, broken project conventions.
   - `blocker`: wrong behaviour, data loss, security, an unmet criterion.
   - `major`: a likely bug, a missing test for a criterion, a clear convention break.
   - `minor`: everything else worth a line. Keep these few.
   Never report style preferences or speculative issues without a concrete failure scenario.
3. `lessons`: only reusable rules this project should remember (kind `gotcha` or `convention`). Usually zero or one.
