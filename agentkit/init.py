"""`agent-kit init`: detect the stack with scripts and write a draft .agent/config.json."""
import json
from pathlib import Path

from . import kb
from .config import DEFAULTS, load
from .tracker import default_branch
from .util import log, main_root, read_json, write_json

CLAUDE_MD = """
## Knowledge base and ticket loop (agent-kit)

- Before exploring the code for a task, run `agent-kit kb context "<task words>"`. It prints the
  relevant files, past tickets and gotchas from `.knowledge/` in a few thousand tokens.
- `.knowledge/tree.md` is the code map; `.knowledge/conventions.md` and `gotchas.md` hold rules
  learned in reviews. Add to them when you learn something reusable.
- Check work with `agent-kit gates` (prints only failures). The ticket loop is `agent-kit run`.
"""


def _pm(root):
    for lock, pm, ci in (("pnpm-lock.yaml", "pnpm", "pnpm install --frozen-lockfile"),
                         ("yarn.lock", "yarn", "yarn install --frozen-lockfile"),
                         ("bun.lockb", "bun", "bun install"), ("bun.lock", "bun", "bun install"),
                         ("package-lock.json", "npm", "npm ci")):
        if (root / lock).exists():
            return pm, ci
    return "npm", "npm install"


def detect(root):
    gates, fix, setup, env, allow = [], [], [], {}, []
    pkg = read_json(root / "package.json")
    if pkg:
        pm, ci = _pm(root)
        scripts = pkg.get("scripts", {})
        run = lambda s: f"{pm} run {s}"
        setup.append(ci)
        allow.append(f"{pm} run *")
        for name in ("format", "format:write", "prettier:write"):
            if name in scripts:
                fix.append({"name": name, "cmd": run(name)})
                break
        for gate, names in (("lint", ["lint"]), ("format", ["format:check", "prettier:check"]),
                            ("typecheck", ["typecheck", "type-check", "tsc", "check-types"]),
                            ("test", ["test:run", "test"])):
            hit = next((s for s in names if s in scripts), None)
            if hit:
                gates.append({"name": gate, "cmd": run(hit)})
    cargo_dirs = [p.parent for p in [root / "Cargo.toml", *root.glob("*/Cargo.toml")] if p.exists()]
    for d in cargo_dirs[:1]:
        rel = str(d.relative_to(root)) or "."
        env["CARGO_TARGET_DIR"] = "{repo}/" + (f"{rel}/" if rel != "." else "") + "target"
        fix.append({"name": "cargo-fmt", "cmd": "cargo fmt", "cwd": rel})
        gates += [{"name": "clippy", "cmd": "cargo clippy --all-targets -- -D warnings", "cwd": rel},
                  {"name": "cargo-test", "cmd": "cargo test", "cwd": rel}]
        allow.append("cargo *")
    py = root / "pyproject.toml"
    if py.exists():
        t = py.read_text()
        if "ruff" in t:
            fix.append({"name": "ruff-format", "cmd": "ruff format ."})
            gates.append({"name": "ruff", "cmd": "ruff check ."})
        if "mypy" in t:
            gates.append({"name": "mypy", "cmd": "mypy ."})
        gates.append({"name": "pytest", "cmd": "pytest -q"})
        allow += ["pytest *", "ruff *", "uv *", "python *"]
    if (root / "go.mod").exists():
        fix.append({"name": "gofmt", "cmd": "gofmt -w ."})
        gates += [{"name": "vet", "cmd": "go vet ./..."}, {"name": "go-test", "cmd": "go test ./..."}]
        allow.append("go *")
    if not gates and (root / "Makefile").exists():
        mk = (root / "Makefile").read_text()
        for t in ("lint", "test"):
            if f"\n{t}:" in "\n" + mk:
                gates.append({"name": t, "cmd": f"make {t}"})
        allow.append("make *")
    return {"gates": gates, "autofix": fix, "setup": setup, "env": env, "allow": allow}


def init(force=False, claude_md=False):
    root = main_root()
    cfgp = root / ".agent" / "config.json"
    if cfgp.exists() and not force:
        log(f"{cfgp} exists (use --force to regenerate)")
    else:
        d = detect(root)
        cfg = {
            "tracker": DEFAULTS["tracker"],
            "branch": {**DEFAULTS["branch"], "base": default_branch(root)},
            "workspace": {"mode": "worktree", "dir": DEFAULTS["workspace"]["dir"], "setup": d["setup"], "env": d["env"]},
            "autofix": d["autofix"],
            "gates": d["gates"],
            "merge": DEFAULTS["merge"],
            "models": DEFAULTS["models"],
            "implement": {"allow_bash": d["allow"]},
            "pr": DEFAULTS["pr"],
        }
        write_json(cfgp, cfg)
        log(f"wrote {cfgp}; review gates and labels, then run `agent-kit kb update`")
        print(json.dumps({k: cfg[k] for k in ("gates", "autofix", "workspace")}, indent=2))
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    want = [".agent/runs/", ".knowledge/index.sqlite"]
    missing = [w for w in want if w not in lines]
    if missing:
        gi.write_text("\n".join(lines + ["", "# agent-kit", *missing]).lstrip("\n") + "\n")
        log(f".gitignore: added {', '.join(missing)}")
    kb.ensure_seeds(load(root), root)
    cm = root / "CLAUDE.md"
    if claude_md and (not cm.exists() or "agent-kit kb context" not in cm.read_text()):
        cm.write_text((cm.read_text() if cm.exists() else "# Agent notes\n") .rstrip("\n") + "\n" + CLAUDE_MD)
        log("CLAUDE.md: added the knowledge base section")
    elif not claude_md:
        log("tip: `agent-kit init --claude-md` adds a short CLAUDE.md section so every session uses the KB")
