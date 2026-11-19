"""The only place that calls an LLM: headless `claude -p` with JSON output.

lean=True replaces the system prompt and drops MCP servers, which cuts a
trivial call from ~50k to ~1k input tokens. Use it for summarize and review.
Every call is appended to a cost log.

stream_log=<path> streams the session instead (stream-json): every event is
written to that file and each tool call is printed as it happens. Same tokens,
same result; only the output format differs.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from .util import append_jsonl, log, read_jsonl


class LLMError(RuntimeError):
    pass


def claude(prompt, *, cwd, step, model=None, schema=None, lean=False, system=None,
           append_system=None, tools=None, permission_mode=None, allow=None, effort=None,
           budget=None, resume=None, persist=False, timeout=3600, cost_log=None, stream_log=None):
    fmt = ["stream-json", "--verbose"] if stream_log else ["json"]
    cmd = ["claude", "-p", "--output-format", *fmt, "--strict-mcp-config"]
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
    if stream_log:
        d = _stream(cmd, prompt, cwd=cwd, step=step, timeout=timeout, path=stream_log)
    else:
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


def _stream(cmd, prompt, *, cwd, step, timeout, path):
    """Run claude with stream-json: log every event, print tool calls live, return the result event."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    fired = threading.Event()

    def kill():
        fired.set()
        proc.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    result, other = None, []
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        with path.open("a") as f:
            for line in proc.stdout:
                f.write(line)
                f.flush()
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    other.append(line)
                    continue
                if isinstance(ev, dict) and ev.get("type") == "result":
                    result = ev
                try:
                    for text in describe(ev, cwd):
                        log(f"  {step} | {text}")
                except Exception as e:  # printing must never stop the session
                    log(f"  {step} | (could not show an event: {type(e).__name__})")
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if fired.is_set():
        raise LLMError(f"{step}: timed out after {timeout}s")
    if result is None:
        raise LLMError(f"{step}: no result from claude (exit {proc.returncode}): {''.join(other)[-1500:]}")
    return result


def _short(value, n=100):
    value = " ".join(str(value).split())
    return value if len(value) <= n else value[: n - 3] + "..."


def _content(ev):
    """The message content blocks of an event; [] for any other shape."""
    msg = ev.get("message") if isinstance(ev, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def describe(ev, cwd=None):
    """One short line per tool call, failed tool result or permission denial in a stream-json event."""
    out = []
    if not isinstance(ev, dict):
        return out
    t = ev.get("type")
    if ev.get("subtype") == "permission_denied":
        return [f"  ! denied {ev.get('tool_name') or 'tool'}: {_short(ev.get('message') or '')}"]
    for c in _content(ev):
        if t == "assistant" and c.get("type") == "tool_use":
            name, inp = c.get("name"), c.get("input") or {}
            if name == "StructuredOutput":
                continue
            arg = inp.get("file_path") or inp.get("path") or inp.get("command") or inp.get("pattern") or ""
            if cwd and isinstance(arg, str) and arg.startswith(str(cwd).rstrip("/") + "/"):
                arg = arg[len(str(cwd).rstrip("/")) + 1:]
            out.append(f"{name} {_short(arg)}".rstrip())
        elif t == "user" and c.get("type") == "tool_result" and c.get("is_error"):
            body = c.get("content")
            if isinstance(body, list):
                body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
            out.append(f"  ! {_short(body)}")
    return out


def render_log(path, cwd=None):
    """Readable lines for a saved stream log: tool calls, errors, and the final result."""
    lines = []
    for raw in Path(path).read_text().splitlines():
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        lines += describe(ev, cwd)
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for c in _content(ev):
                if c.get("type") == "text" and c.get("text", "").strip():
                    lines.append(f"  > {_short(c['text'], 160)}")
        if ev.get("type") == "result":
            lines.append(f"= {'error' if ev.get('is_error') else 'done'}: turns={ev.get('num_turns')} "
                         f"${round(ev.get('total_cost_usd') or 0, 4)} "
                         f"denied={len(ev.get('permission_denials') or [])}")
    return lines


def step_kind(row):
    """A cost row's step without its round number: "fix-gates:2" -> "fix-gates"."""
    return (row.get("step") or "").split(":")[0]


def cost_summary(path):
    if not Path(path).exists():
        return None
    by = {}
    for r in read_jsonl(path):
        s = by.setdefault(step_kind(r), {"calls": 0, "usd": 0.0, "secs": 0.0})
        s["calls"] += 1
        s["usd"] += r.get("usd") or 0
        s["secs"] += r.get("secs") or 0
    return by
