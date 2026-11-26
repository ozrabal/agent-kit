"""Setup checks before a run: tools, auth, config, tracker labels, base branch, gates, knowledge base.
No LLM. Each check returns results {name, ok, warn, hint}; the hint says how to fix a failure.
A warning (ok but worth a look) does not fail doctor."""
import json
import sys
import time

from . import hooks, kb, retro, tdd
from .config import DEFAULTS, RETRO_LOG, config_path, runs_dir, unknown_keys
from .util import read_json, read_jsonl, sh

TOOLS = {
    "git": "install git",
    "gh": "install the GitHub CLI: https://cli.github.com",
    "claude": "install Claude Code: https://docs.claude.com/en/docs/claude-code",
}


def result(name, ok, hint=""):
    return {"name": name, "ok": bool(ok), "warn": False, "hint": "" if ok else hint}


def warning(name, hint):
    return {"name": name, "ok": True, "warn": True, "hint": hint}


def _succeeds(run, cmd, cwd=None):
    """True when the command exits 0; False when it fails or the binary is missing."""
    try:
        return run(cmd, cwd=cwd, check=False).returncode == 0
    except (FileNotFoundError, OSError):
        return False


def check_tools(ctx):
    return [result(t, _succeeds(ctx.run, [t, "--version"]), hint) for t, hint in TOOLS.items()]


def check_python(ctx):
    return [result("python", sys.version_info >= (3, 10), "agent-kit needs Python 3.10 or newer")]


def check_gh_auth(ctx):
    return [result("gh auth", _succeeds(ctx.run, ["gh", "auth", "status"]), "run `gh auth login`")]


def check_config(ctx):
    path = config_path(ctx.cfg["_root"])
    if not path.exists():
        return [result("config", False, "run `agent-kit init`")]
    user = read_json(path)
    if not isinstance(user, dict):
        return [result("config", False, f"{path} is not a JSON object")]
    bad = unknown_keys(user, DEFAULTS)
    return [warning("config", "unknown keys (typo?): " + ", ".join(bad)) if bad else result("config", True)]


def check_labels(ctx):
    if ctx.cfg["tracker"]["type"] != "github":
        return []
    r = ctx.run(["gh", "label", "list", "--limit", "500", "--json", "name"], cwd=ctx.cfg["_root"], check=False)
    try:
        have = {x["name"] for x in json.loads(r.stdout or "[]")} if r.returncode == 0 else None
    except json.JSONDecodeError:
        have = None
    if have is None:
        return [result("labels", False, "could not list labels; check `gh auth status` and the repo remote")]
    missing = [l for l in ctx.cfg["tracker"]["labels"].values() if l not in have]
    return [result("labels", not missing, "create the missing labels: " + ", ".join(missing))]


def check_base_branch(ctx):
    remote, base = ctx.cfg["branch"]["remote"], ctx.cfg["branch"]["base"]
    r = ctx.run(["git", "ls-remote", "--heads", remote, base], cwd=ctx.cfg["_root"], check=False)
    ok = r.returncode == 0 and bool((r.stdout or "").strip())
    return [result("base branch", ok, f"{remote}/{base} not found; set branch.base / branch.remote in .agent/config.json")]


def check_gates(ctx):
    if not ctx.cfg["gates"]:
        return [result("gates", False, "no gates configured: add lint/test commands to `gates` in .agent/config.json")]
    if ctx.run_gates is None:
        return [result("gates", True)]
    rep = ctx.run_gates()
    failed = [r["name"] for r in rep["results"] if not r["ok"]]
    return [result("gates", not failed, "failing on this checkout: " + ", ".join(failed)
                   + " (run `agent-kit gates` for details)")]


def check_knowledge_base(ctx):
    ok = (kb.kdir(ctx.cfg, ctx.cfg["_root"]) / "files.jsonl").exists()
    return [result("knowledge base", ok, "run `agent-kit kb update`")]


def check_hooks(ctx):
    paths = [hooks.settings_path(ctx.cfg["_root"]), ctx.user_settings]
    gaps = [hooks.missing(read_json(p, {})) for p in paths]  # per settings file
    if not all(gaps):  # one file has every hook
        return [result("hooks", True)]
    closest = min(gaps, key=len)
    return [warning("hooks", "agent-kit hooks missing for " + ", ".join(closest) + ": run "
                    "`agent-kit hooks install` and commit .claude/settings.json")]


def _recent(ctx, keep):
    """Hook log rows within hooks.error_window_hours that keep(row) accepts, and the window in hours."""
    hours = ctx.cfg["hooks"]["error_window_hours"]
    since = time.time() - hours * 3600
    return [r for r in hooks.read_rows(ctx.cfg) if keep(r) and (r.get("ts") or 0) >= since], hours


def check_hook_errors(ctx):
    """Hooks never fail a session; their internal errors only show up in .agent/runs/hooks.jsonl."""
    errs, hours = _recent(ctx, lambda r: r.get("outcome") == hooks.ERROR)
    if not errs:
        return [result("hook errors", True)]
    last = errs[-1]
    return [warning("hook errors", f"{len(errs)} in the last {hours}h; latest ({last.get('event')} in "
                                   f"{last.get('cwd') or '?'}): {last.get('note')}")]


def check_guard(ctx):
    """Guard denials are not errors, but each one is a session that tried to leave its scope."""
    denied, hours = _recent(ctx, lambda r: r.get("event") == "guard" and r.get("outcome") == hooks.BLOCK)
    if not denied:
        return [result("guard denials", True)]
    last = denied[-1]
    return [warning("guard denials", f"{len(denied)} in the last {hours}h; latest in {last.get('cwd') or '?'}: "
                                     f"{last.get('note')} (`agent-kit log <issue>` shows the run)")]


def check_tdd(ctx):
    """loop.tdd needs a gate to run the tests on the base; without one the check is always skipped."""
    t = ctx.cfg["loop"]["tdd"]
    if not t["enabled"]:
        return []
    if tdd.gate(ctx.cfg):
        return [result("tdd", True)]
    return [result("tdd", False, f"loop.tdd.gate '{t['gate']}' is not a gate name" if t["gate"] else
                   "no gate name contains 'test': set loop.tdd.gate to the gate that runs the tests")]


def check_loop_modes(ctx):
    """loop.refine is a known mode; loop.parallel runs need their own worktrees and PR slots."""
    lp, res = ctx.cfg["loop"], []
    mode = lp["refine"]
    res.append(result("refine", mode in ("off", "check", "auto"), f"loop.refine must be off, check or auto (got {mode!r})"))
    par, most = lp["parallel"], ctx.cfg["merge"]["max_open_prs"]
    if not isinstance(par, int) or isinstance(par, bool) or par < 1:
        res.append(result("parallel", False, f"loop.parallel must be a whole number >= 1 (got {par!r})"))
    elif par > 1 and ctx.cfg["workspace"]["mode"] != "worktree":
        res.append(result("parallel", False, "loop.parallel > 1 needs workspace.mode \"worktree\" (one checkout per ticket)"))
    elif par > most:
        res.append(warning("parallel", f"loop.parallel {par} > merge.max_open_prs {most}: at most {most} run at once"))
    else:
        res.append(result("parallel", True))
    q = lp["questions"]
    rounds_ok = isinstance(q.get("max_rounds"), int) and not isinstance(q.get("max_rounds"), bool) and q["max_rounds"] >= 1
    res.append(result("questions", isinstance(q.get("enabled"), bool) and rounds_ok,
                      f"loop.questions needs enabled true/false and max_rounds >= 1 (got {q!r})"))
    mg = ctx.cfg["merge"]
    if mg["policy"] not in ("pause", "auto") or mg["method"] not in ("merge", "squash", "rebase"):
        res.append(result("merge", False, f"merge.policy must be pause or auto (got {mg['policy']!r}), "
                                          f"merge.method merge, squash or rebase (got {mg['method']!r})"))
    elif mg["policy"] == "auto" and not mg["require_checks"]:
        res.append(warning("merge", "merge.policy auto with require_checks false: a PR with no CI checks "
                                    "(or before they start) is merged on local gates and review only"))
    else:
        res.append(result("merge", True))
    return res


def check_retro(ctx):
    """Repeated gate errors, findings or denials in the runs since the last retro that asked the LLM
    (or found nothing to learn): a sign the same mistakes cost tokens again."""
    done = [x for x in read_jsonl(runs_dir(ctx.cfg) / RETRO_LOG) if x.get("mode") in ("llm", "nothing")]
    since = done[-1].get("ts") if done else None
    agg = retro.scan(ctx.cfg, since)
    if not retro.repeated(agg):
        return [result("retro", True)]
    return [warning("retro", f"in {agg['runs']} runs {'since the last retro' if since else 'and no retro yet'}: "
                             f"{len(agg['errors'])} repeated gate errors, {len(agg['findings'])} review findings, "
                             f"{len(retro.repeated_denials(agg))} denied tools: run `agent-kit retro`")]


CHECKS = [check_tools, check_python, check_gh_auth, check_config, check_labels, check_base_branch,
          check_gates, check_tdd, check_loop_modes, check_knowledge_base, check_hooks,
          check_hook_errors, check_guard, check_retro]


class Ctx:
    def __init__(self, cfg, run, run_gates, user_settings):
        self.cfg, self.run, self.run_gates, self.user_settings = cfg, run, run_gates, user_settings


def check(cfg, run=sh, run_gates=None, user_settings=None):
    """Run every check in CHECKS. run_gates: callable returning a gates report, or None to skip gates.
    user_settings: the user's Claude settings.json (default ~/.claude/settings.json)."""
    ctx = Ctx(cfg, run, run_gates, user_settings or hooks.settings_path())
    return [r for c in CHECKS for r in c(ctx)]


def all_ok(results):
    return all(r["ok"] for r in results)


def render(results):
    def line(r):
        if not r["ok"]:
            return f"FAIL {r['name']} -> {r['hint']}"
        return f"WARN {r['name']} -> {r['hint']}" if r["warn"] else f"OK   {r['name']}"
    return "\n".join(map(line, results))
