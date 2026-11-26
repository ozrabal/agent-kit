"""Per-project config: kit defaults deep-merged with <repo>/.agent/config.json."""
import copy
from pathlib import Path

from .util import main_root, read_json

READ_ONLY_BASH = [
    "agent-kit *", "git status*", "git diff*", "git log*", "git show*", "git ls-files*",
    "rg *", "grep *", "find *", "ls *", "cat *", "head *", "tail *", "wc *", "cd *",
]

DEFAULTS = {
    "tracker": {
        "type": "github",
        "labels": {"ready": "ready-for-agent", "wip": "in-progress", "done": "done", "needs_human": "needs-human",
                   "refine": "needs-refinement"},
        "blocked_by_heading": "blocked by",
        "key_pattern": r"^([A-Za-z][A-Za-z0-9]*-\d+[a-z]?)\b",
        "closed_counts_as_done": True,
    },
    "branch": {"base": "main", "remote": "origin", "name": "{key}-{slug}"},
    "workspace": {
        "mode": "worktree",  # worktree | branch
        "dir": "{repo}/../.agent-wt/{repo_name}",
        "setup": [],
        "env": {},
    },
    "autofix": [],
    "gates": [],
    "file_gates": [],  # {"name", "match": glob, "cmd": "... {file}", "timeout"}: run by the post-edit hook
    "hooks": {"stop_gate": True, "stop_max_blocks": 2, "file_gate_timeout": 60, "note_chars": 600,
              "error_window_hours": 24},
    "guard": {  # PreToolUse scope guard; active during a loop run, or always when "always" is true
        "always": False,
        "protected": [".git/*", ".agent/*", ".github/workflows/*", ".claude/*"],
        "deny_bash": [
            r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b", r"\bgh\s+pr\s+merge\b",
            r"\brm\s+(-\S+\s+)*(/|~/?|\$HOME/?)(\*|\s|$)",  # rm -rf / or ~
            r"\bcurl\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",  # curl ... | sh
            r">>?\s*(~|\$HOME)/", r"\btee\b[^;&|]*\s(~|\$HOME)/",  # writes to ~/
            r"\b(cp|mv)\b[^;&|]*\s(~|\$HOME)(/\S*)?\s*($|[;&|])",
        ],
    },
    # policy "pause": a human merges; "auto": a follow-up poll (run --wait, address) merges a PR once CI is green,
    # nothing is new or pending and no review asks for changes (gh pr merge --<method>; no LLM call)
    "merge": {"policy": "pause", "method": "squash", "require_checks": True, "max_open_prs": 1, "poll_seconds": 120},
    "loop": {
        # before implement: "off" | "check" (script check, skip failing tickets) | "auto" (+ one lean LLM proposal)
        "refine": "check",
        # tickets at the same time (each its own worktree and process), never more than merge.max_open_prs open PRs
        "parallel": 1,
        "max_fix_rounds": 2, "max_review_rounds": 2, "timeout_seconds": 3600,
        "max_ci_rounds": 2, "max_comment_rounds": 2,  # per PR, after it is open (agent-kit address / run --wait)
        # red/green: the branch's new tests must fail on the base; gate null = first gate named *test*
        "tdd": {"enabled": False, "gate": None,
                "test_paths": ["tests/**", "**/*_test.*", "**/test_*.*", "**/*.test.*", "**/*.spec.*"]},
    },
    # escalate: model for the last gate-fix and the last review-fix round (null = models.implement)
    "models": {"implement": "sonnet", "review": "opus", "review_light": "sonnet", "summarize": "haiku",
               "triage": "haiku", "escalate": None, "refine": "sonnet", "retro": "sonnet"},
    "effort": {"implement": None, "review": None, "summarize": "low", "retro": None},
    "budget_usd": {"implement": 8, "fix": 3, "review": 3, "summarize": 0.5, "triage": 0.3, "refine": 0.5, "retro": 0.5},
    # agent-kit retro: a group is "repeated" in >= min_runs runs; error_lines per failing gate; top items
    # per report section; max_chars of the report and known_chars of the known rules/gotchas sent to the one LLM call
    "retro": {"min_runs": 2, "error_lines": 3, "top": 10, "max_chars": 12000, "known_chars": 3000},
    # the script check: items under criteria_heading; max_chars 0 = no limit; above max_criteria = too big
    "refine": {"criteria_heading": "acceptance criteria", "max_chars": 8000, "max_criteria": 8},
    "implement": {
        "permission_mode": "acceptEdits",
        "tools": "Bash,Read,Edit,Write,Glob,Grep",
        "allow_bash": [],
    },
    "review": {
        "tools": "Read,Glob,Grep",
        "diff_chars": 60000,
        "block_on": ["blocker", "major"],
        "exclude": ["*.lock", "*-lock.json", "*.lock.json", ".knowledge/*", "*.snap", "*.min.*"],
        "skip_paths": ["*.md", "docs/**", "*.txt"],  # every changed file matches: no review
        "skip_under_lines": 0,  # skip at or under this many lines (0 = never), unless criteria
        "risky_paths": [],  # any changed file matches: models.review
        "light_max_lines": 300,  # above this: models.review, else models.review_light
    },
    "pr": {"sections": ["Summary", "Evidence"], "include_review": True, "draft_on_fail": True,
           "post_replies": False,
           # only comments by these GitHub author associations are triaged (anyone else's could steer the agent)
           "trusted_associations": ["OWNER", "MEMBER", "COLLABORATOR"]},  # post triage reply drafts on the PR (else only .agent/runs/<n>/replies.md)
    "kb": {
        "dir": ".knowledge",
        "include_ext": [
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".py", ".rs", ".go",
            ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".sql",
            ".sh", ".md", ".toml", ".yml", ".yaml", ".css", ".scss",
        ],
        "exclude": [
            "*.lock", "*-lock.*", "*.min.*", "dist/*", "build/*", "vendor/*", "node_modules/*",
            "*/gen/*", "*.generated.*", ".knowledge/*", ".agent/*", "CHANGELOG.md",
        ],
        "max_file_kb": 200,
        "llm_summaries": True,
        "batch_chars": 40000,
        "context_chars": 12000,
        "context_files": 12,
    },
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


# Run statuses while the loop's Claude session works (set in loop.py); the guard is active only then.
IMPLEMENTING, FIXING_REVIEW = "implementing", "fixing-review-"


def config_path(root):
    return Path(root) / ".agent" / "config.json"


RETRO_LOG, RETRO_COST_LOG = "retro.jsonl", "retro-cost.jsonl"  # in runs_dir: one row per retro; its LLM calls


def runs_dir(cfg):
    """Local run state: .agent/runs/<issue>/ plus hooks.jsonl, kb-cost.jsonl, retro.jsonl and retro-cost.jsonl."""
    return Path(cfg["_root"]) / ".agent" / "runs"


def hook_state_path(cfg):
    """Stop-hook block counters per Claude session (local, gitignored)."""
    return Path(cfg["_root"]) / ".agent" / "hook-state.json"


def unknown_keys(user, defaults, prefix=""):
    """Dotted paths of keys in user that DEFAULTS does not know. An empty dict in DEFAULTS
    (like workspace.env) is a free-form map, so its keys are not checked."""
    found = []
    for k, v in user.items():
        path = prefix + k
        if k not in defaults:
            found.append(path)
        elif isinstance(v, dict) and isinstance(defaults[k], dict) and defaults[k]:
            found += unknown_keys(v, defaults[k], path + ".")
    return found


def load(cwd=None):
    root = main_root(cwd)
    user = read_json(config_path(root), {})
    cfg = _merge(copy.deepcopy(DEFAULTS), user)
    cfg["_root"] = str(root)
    cfg["_configured"] = bool(user)
    return cfg


def fmt(s, cfg, **extra):
    """Expand {repo}, {repo_name} and extra placeholders in config strings."""
    root = Path(cfg["_root"])
    vals = {"repo": str(root), "repo_name": root.name, **extra}
    try:
        return s.format(**vals)
    except (KeyError, IndexError):
        return s
