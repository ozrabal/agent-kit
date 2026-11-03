# agent-kit — agent notes

A stack-independent CLI that runs a ticket loop and keeps a project knowledge base. Design rule:
scripts do everything that needs no judgment; the LLM is called only to implement, fix, review
and summarize. Read `README.md` for the flow.

## Constraints

- Python 3.10+ standard library only. No third-party packages. External tools: `git`, `gh`, `claude`.
- Every LLM call goes through `agentkit/llm.py` (`claude -p`) so it is logged with cost. Use
  `lean=True` (replaced system prompt, no MCP) unless the call must edit code.
- Nothing project-specific in the kit. Project differences go in `.agent/config.json`, with
  defaults in `agentkit/config.py`.
- Keep token use visible: a change that adds an LLM call or grows a prompt must say why in the PR.

## Layout

- `agentkit/cli.py`: commands. `loop.py`: ticket loop. `kb.py`: knowledge base.
  `tracker.py`: issue tracker adapters. `gates.py`: gates and autofix. `prompts/`: LLM prompts.
- `skills/agent-kit/SKILL.md`: the Claude skill, linked into `~/.claude/skills` by `install.sh`.

## Checks

```sh
python3 -m unittest discover -s tests
python3 -m py_compile agentkit/*.py
```

Try changes against the sandbox (`sandbox/README.md`), never a real project: `python3
sandbox/reset.py`, then `agent-kit run --dry-run` and `agent-kit kb context '#<n>'` in
`~/agent-kit-sandbox/repo` before anything that calls the LLM.
