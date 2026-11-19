"""Claude Code hooks: `agent-kit hook <event>` reads the hook's stdin JSON and runs gates inside
the session, so Claude fixes errors while it still has the context. No LLM.

Exit 2 blocks (Claude sees the message); anything else is exit 0. A hook never crashes a session:
an internal error is exit 0 with a short note."""
import fnmatch
import json
import shlex
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, gates
from .util import append_jsonl, log, read_json, repo_root, sh, write_json


def _load(cwd):
    try:
        cfg = config.load(cwd)
    except RuntimeError:  # not a git repo
        return None
    return cfg if cfg["_configured"] else None


def _repo(cwd):
    """The checkout Claude works in (a worktree in the loop); cwd itself outside git."""
    try:
        return repo_root(cwd)
    except (RuntimeError, OSError):
        return Path(cwd)


def _outcome(outcome, code=0, msg="", **row):
    """What a handler returns: exit code, stderr message, and the fields for the hook log row."""
    return {"code": code, "msg": msg, "outcome": outcome, **row}


def _gates_row(report):
    return [{k: r[k] for k in ("name", "ok", "secs")} for r in report["results"]]


def post_edit(cfg, data, run=sh):
    """PostToolUse on Edit|Write|MultiEdit: run the file_gates whose `match` glob fits the edited file."""
    path = (data.get("tool_input") or {}).get("file_path")
    if not path:
        return _outcome("skip", note="no file_path in tool_input")
    if not cfg["file_gates"]:
        return _outcome("skip", file=path, note="no file_gates")
    root = _repo(data.get("cwd") or cfg["_root"]).resolve()
    try:
        rel = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return _outcome("skip", file=path, note="outside the repo")
    env, results = gates.env(cfg), []
    for g in cfg["file_gates"]:
        if fnmatch.fnmatch(rel, g["match"]):
            g = {**g, "name": f"{g['name']} {rel}", "cmd": g["cmd"].replace("{file}", shlex.quote(rel))}
            results.append(gates.run_one(g, root, env, run=run, timeout=60))
    if not results:
        return _outcome("skip", file=rel, note="no file_gates match")
    rep = {"results": results}
    failed = {"results": [r for r in results if not r["ok"]]}
    if not failed["results"]:
        return _outcome("pass", file=rel, gates=_gates_row(rep))
    text = gates.render(failed)
    return _outcome("block", 2, text, file=rel, gates=_gates_row(rep), note=text)


def _check(cfg, cwd):
    gates.autofix(cfg, cwd)
    return gates.run(cfg, cwd)


def stop(cfg, data, check=_check):
    """Stop: block while gates fail, but at most hooks.stop_max_blocks times in a row per session."""
    h = cfg["hooks"]
    if not h["stop_gate"]:
        return _outcome("skip", note="hooks.stop_gate is off")
    path, sid = config.hook_state_path(cfg), data.get("session_id") or "-"
    state = read_json(path, {}) or {}
    blocks = state.get(sid, 0) if data.get("stop_hook_active") else 0
    if blocks >= h["stop_max_blocks"]:  # kept until a fresh stop (stop_hook_active false) resets it
        return _outcome("give_up", blocks=blocks, note=f"gates still fail after {blocks} blocks; letting the session stop")
    rep = check(cfg, data.get("cwd") or cfg["_root"])
    if rep["ok"]:
        if sid in state:
            state.pop(sid)
            write_json(path, state)
        return _outcome("pass", gates=_gates_row(rep), blocks=blocks)
    state[sid] = blocks + 1
    write_json(path, state)
    text = gates.render(rep)
    return _outcome("block", 2, "Gates fail, fix before finishing:\n" + text,
                    gates=_gates_row(rep), blocks=blocks + 1, note=text)


EVENTS = {"post-edit": post_edit, "stop": stop}

# What `agent-kit hooks install` writes into .claude/settings.json: {Claude event: (matcher, our event)}.
SETTINGS = {"PostToolUse": ("Edit|Write|MultiEdit", "post-edit"), "Stop": (None, "stop")}


def command(event):
    return f"agent-kit hook {event}"


def _ours(h):
    return h.get("command") in {command(e) for e in EVENTS}


def unmerge(settings):
    """settings without agent-kit's hooks; empty groups and events are dropped."""
    hk = settings.get("hooks") or {}
    for event in list(hk):
        groups = []
        for g in hk[event]:
            g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h)]
            if g["hooks"]:
                groups.append(g)
        if groups:
            hk[event] = groups
        else:
            del hk[event]
    if "hooks" in settings and not hk:
        del settings["hooks"]
    return settings


def merge(settings):
    """settings with agent-kit's hooks added once; other hooks and keys are kept."""
    settings = unmerge(settings)
    hk = settings.setdefault("hooks", {})
    for event, (matcher, ours) in SETTINGS.items():
        group = {"matcher": matcher} if matcher else {}
        group["hooks"] = [{"type": "command", "command": command(ours)}]
        hk.setdefault(event, []).append(group)
    return settings


def settings_path(root=None):
    """<root>/.claude/settings.json, or the user's ~/.claude/settings.json when root is None."""
    return (Path(root) if root else Path.home()) / ".claude" / "settings.json"


def installed(settings):
    hk = (settings or {}).get("hooks") or {}
    return all(any(h.get("command") == command(ours) for g in hk.get(event, []) for h in g.get("hooks", []))
               for event, (_, ours) in SETTINGS.items())


def render_row(row, cwd=None):
    """One readable line for a hook log row, for `agent-kit log` and the live run output."""
    f = row.get("file") or ""
    if cwd and f.startswith(str(cwd).rstrip("/") + "/"):
        f = f[len(str(cwd).rstrip("/")) + 1:]
    head = f"hook {row.get('event')}{' ' + f if f else ''}: {str(row.get('outcome')).upper()}"
    if row.get("event") == "stop" and row.get("outcome") in ("block", "give_up"):
        head += f" #{row.get('blocks')}"
    parts = [head] + [f"{g['name']}={'ok' if g['ok'] else 'FAIL'}" for g in row.get("gates") or []]
    line = " ".join(parts) + f" ({row.get('secs')}s)"
    note = (row.get("note") or "").strip().splitlines()
    return line + (f" | {note[0]}" if note else "")


def log_path(cfg):
    return config.runs_dir(cfg) / "hooks.jsonl"


def in_worktree(row, ws):
    """True when a hook row came from a session working in ws (or a folder inside it)."""
    cwd, ws = row.get("cwd"), str(ws or "")
    return bool(cwd and ws) and (cwd == ws or cwd.startswith(ws.rstrip("/") + "/"))


@contextmanager
def watch(cfg, ws, step, every=1.0):
    """While the block runs, print each new hook row from ws as `<step> | hook ...` (live run output)."""
    path, done = log_path(cfg), threading.Event()
    start = path.stat().st_size if path.exists() else 0

    def tail():
        pos = start
        while True:
            stop = done.wait(every)
            try:
                with path.open() as f:
                    f.seek(pos)
                    chunk = f.read()
            except FileNotFoundError:
                chunk = ""
            whole = chunk[: chunk.rfind("\n") + 1]  # only complete lines
            pos += len(whole.encode())
            for line in whole.splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if in_worktree(row, ws):
                    log(f"  {step} | {render_row(row, cwd=ws)}")
            if stop:
                return

    t = threading.Thread(target=tail, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join(timeout=5)


NOTE_CHARS = 600


def _log(cfg, event, data, res, t0):
    row = {"ts": int(t0), "event": event, "session": data.get("session_id"), "cwd": data.get("cwd"),
           **{k: v for k, v in res.items() if k not in ("code", "msg")}, "secs": round(time.time() - t0, 2)}
    if row.get("note"):
        row["note"] = row["note"][:NOTE_CHARS]
    append_jsonl(log_path(cfg), row)


def main(event, raw, load=_load, **deps):
    """(exit code, message for stderr). Every call with a config is logged to .agent/runs/hooks.jsonl.
    deps are passed to the event handler (fakes in tests)."""
    t0, cfg, data = time.time(), None, {}
    try:
        data = json.loads(raw or "{}")
        handler = EVENTS.get(event)
        if handler is None:
            return 0, ""
        cfg = load(data.get("cwd"))
        if cfg is None:
            return 0, ""
        res = handler(cfg, data, **deps)
    except Exception as e:  # a hook must never break the session
        res = _outcome("error", note=f"{type(e).__name__}: {str(e)[:300]}")
    msg = res["msg"] or (f"agent-kit hook {event}: {res['note']}" if res["outcome"] in ("error", "give_up") else "")
    if cfg is not None:
        try:
            _log(cfg, event, data, res, t0)
        except Exception:  # logging must not break the session either
            pass
    return res["code"], msg


def install(path):
    write_json(path, merge(read_json(path, {}) or {}))
    log(f"hooks installed in {path}")


def uninstall(path):
    if path.exists():
        write_json(path, unmerge(read_json(path, {}) or {}))
        log(f"agent-kit hooks removed from {path}")
