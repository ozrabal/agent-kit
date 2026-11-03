"""The only place that calls an LLM: headless `claude -p` with JSON output.

lean=True replaces the system prompt and drops MCP servers, which cuts a
trivial call from ~50k to ~1k input tokens. Use it for summarize and review.
Every call is appended to a cost log.
"""
import json
import subprocess
import time
from pathlib import Path

from .util import append_jsonl, log


class LLMError(RuntimeError):
    pass


def claude(prompt, *, cwd, step, model=None, schema=None, lean=False, system=None,
           append_system=None, tools=None, permission_mode=None, allow=None, effort=None,
           budget=None, resume=None, persist=False, timeout=3600, cost_log=None):
    cmd = ["claude", "-p", "--output-format", "json", "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if lean:
        cmd += ["--system-prompt", system or "You are a precise, terse assistant."]
    elif append_system:
        cmd += ["--append-system-prompt", append_system]
    if tools is not None:
        cmd += ["--tools", tools]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    if resume:
        cmd += ["--resume", resume]
    if not persist and not resume:
        cmd += ["--no-session-persistence"]
    if allow:
        cmd += ["--allowedTools", *allow]  # variadic: keep last; prompt goes via stdin

    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, input=prompt, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LLMError(f"{step}: timed out after {timeout}s")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"{step}: no JSON from claude (exit {r.returncode}): {(r.stderr or r.stdout)[-1500:]}")

    u = d.get("usage") or {}
    row = {
        "ts": int(t0), "step": step, "model": model, "secs": round(time.time() - t0, 1),
        "usd": round(d.get("total_cost_usd") or 0, 4), "turns": d.get("num_turns"),
        "in": u.get("input_tokens"), "cache_w": u.get("cache_creation_input_tokens"),
        "cache_r": u.get("cache_read_input_tokens"), "out": u.get("output_tokens"),
        "error": d.get("is_error"), "denied": len(d.get("permission_denials") or []),
    }
    if cost_log:
        append_jsonl(cost_log, row)
    log(f"{step}: {row['secs']}s ${row['usd']} turns={row['turns']}"
        + (f" denied={row['denied']}" if row["denied"] else ""))
    if row["denied"]:
        for den in d["permission_denials"][:5]:
            log(f"  denied: {json.dumps(den.get('tool_input', den))[:200]}")
    if d.get("is_error"):
        raise LLMError(f"{step}: {str(d.get('result'))[:1500]}")
    result = d.get("structured_output") if schema else d.get("result")
    if schema and result is None:
        raise LLMError(f"{step}: no structured output returned")
    return result, d.get("session_id")


def cost_summary(path):
    p = Path(path)
    if not p.exists():
        return None
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    by = {}
    for r in rows:
        s = by.setdefault(r["step"].split(":")[0], {"calls": 0, "usd": 0.0, "secs": 0.0})
        s["calls"] += 1
        s["usd"] += r.get("usd") or 0
        s["secs"] += r.get("secs") or 0
    return by
