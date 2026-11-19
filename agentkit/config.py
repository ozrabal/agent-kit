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
        "labels": {"ready": "ready-for-agent", "wip": "in-progress", "done": "done"},
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
    "hooks": {"stop_gate": True, "stop_max_blocks": 2},
    "merge": {"policy": "pause", "max_open_prs": 1, "poll_seconds": 120},
    "loop": {"max_fix_rounds": 2, "max_review_rounds": 2, "timeout_seconds": 3600},
    "models": {"implement": "sonnet", "review": "opus", "summarize": "haiku"},
    "effort": {"implement": None, "review": None, "summarize": "low"},
    "budget_usd": {"implement": 8, "fix": 3, "review": 3, "summarize": 0.5},
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
    },
    "pr": {"sections": ["Summary", "Evidence"], "include_review": True, "draft_on_fail": True},
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


def config_path(root):
    return Path(root) / ".agent" / "config.json"


def runs_dir(cfg):
    """Local run state: .agent/runs/<issue>/ plus kb-cost.jsonl."""
    return Path(cfg["_root"]) / ".agent" / "runs"


def hook_state_path(cfg):
    """Stop-hook block counters per Claude session (local, gitignored)."""
    return Path(cfg["_root"]) / ".agent" / "hook-state.json"


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
