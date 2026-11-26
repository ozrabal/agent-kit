Implement the ticket at the end of this message.

## How to work
- The context pack below was built from the project knowledge base. Start from those files instead of exploring the whole repo; open others only when needed.
- Keep the change scoped to this ticket. No drive-by refactors, no unrelated files.
- Add or update tests that prove each acceptance criterion.
- Check your work with `agent-kit gates` (all gates) or `agent-kit gates <name>` (one gate). It prints only failures. Gates: $gate_names. Formatters run automatically before gates, so do not fix formatting by hand.
- Do not commit, push, open PRs or change issue labels. The driver does that.
- If the ticket is ambiguous, choose the reading that best fits the acceptance criteria and say so in `concerns`.$questions

## Return
- `summary`: 2-5 plain sentences on what changed and why.
- `pr_sections`: one entry per heading in [$pr_sections]; markdown body for each. Be concrete: name files, tests and behaviour.
- `lessons`: only non-obvious, reusable pitfalls or project rules you learned (often none). Not a changelog.
- `concerns`: anything a reviewer or the human should know (often none).

## Ticket $key (issue #$number): $title
$body

$context
