"""Quality gates and autofix, run as plain commands. Failures are compacted so an
LLM fix round only sees the lines that matter."""
import time
from pathlib import Path

from .config import fmt
from .util import compact, env_with, log, sh


def _norm(g):
    return g if isinstance(g, dict) else {"name": g.split()[-1], "cmd": g}


def env(cfg):
    return env_with({k: fmt(v, cfg) for k, v in cfg["workspace"]["env"].items()})


def autofix(cfg, cwd):
    for g in map(_norm, cfg["autofix"]):
        r = sh(g["cmd"], cwd=Path(cwd) / g.get("cwd", "."), check=False, env=env(cfg), timeout=900)
        if r.returncode != 0:
            log(f"autofix '{g['name']}' exited {r.returncode} (ignored)")


def run_one(g, cwd, env, run=sh, timeout=1800):
    """One gate {name, cmd, cwd?, timeout?} -> {name, ok, secs, output (only on failure)}."""
    t0 = time.time()
    r = run(g["cmd"], cwd=Path(cwd) / g.get("cwd", "."), check=False, env=env,
            timeout=g.get("timeout", timeout))
    res = {"name": g["name"], "ok": r.returncode == 0, "secs": round(time.time() - t0, 1)}
    if not res["ok"]:
        res["output"] = compact((r.stdout or "") + "\n" + (r.stderr or ""))
    return res


def run(cfg, cwd, only=None, fail_fast=False):
    results, e = [], env(cfg)
    for g in map(_norm, cfg["gates"]):
        if only and g["name"] not in only:
            continue
        results.append(run_one(g, cwd, e))
        if fail_fast and not results[-1]["ok"]:
            break
    return {"ok": all(r["ok"] for r in results), "results": results}


def render(report, verbose=True):
    lines = []
    for r in report["results"]:
        lines.append(f"{'PASS' if r['ok'] else 'FAIL'} {r['name']} ({r['secs']}s)")
        if not r["ok"] and verbose:
            lines += ["    " + l for l in r["output"].splitlines()]
    if not report["results"]:
        lines.append("no gates configured (.agent/config.json -> gates)")
    return "\n".join(lines)
