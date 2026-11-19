"""Claude Code hooks: `agent-kit hook <event>` reads the hook's stdin JSON and runs gates inside
the session, so Claude fixes errors while it still has the context. No LLM.

Exit 2 blocks (Claude sees the message); anything else is exit 0. A hook never crashes a session:
an internal error is exit 0 with a short note."""
import fnmatch
import json
import shlex
from pathlib import Path

from . import config, gates
from .util import compact, log, read_json, repo_root, sh, write_json


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


def post_edit(cfg, data, run=sh):
    """PostToolUse on Edit|Write|MultiEdit: run the file_gates whose `match` glob fits the edited file."""
    path = (data.get("tool_input") or {}).get("file_path")
    if not cfg["file_gates"] or not path:
        return 0, ""
    root = _repo(data.get("cwd") or cfg["_root"]).resolve()
    try:
        rel = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return 0, ""
    env, failed = gates.env(cfg), []
    for g in cfg["file_gates"]:
        if fnmatch.fnmatch(rel, g["match"]):
            g = {**g, "name": f"{g['name']} {rel}", "cmd": g["cmd"].replace("{file}", shlex.quote(rel))}
            r = gates.run_one(g, root, env, run=run, timeout=60)
            if not r["ok"]:
                failed.append(r)
    return (2, gates.render({"results": failed})) if failed else (0, "")


def _check(cfg, cwd):
    gates.autofix(cfg, cwd)
    return gates.run(cfg, cwd)


def stop(cfg, data, check=_check):
    """Stop: block while gates fail, but at most hooks.stop_max_blocks times in a row per session."""
    h = cfg["hooks"]
    if not h["stop_gate"]:
        return 0, ""
    path, sid = config.hook_state_path(cfg), data.get("session_id") or "-"
    state = read_json(path, {}) or {}
    blocks = state.get(sid, 0) if data.get("stop_hook_active") else 0
    if blocks >= h["stop_max_blocks"]:  # kept until a fresh stop (stop_hook_active false) resets it
        return 0, f"agent-kit: gates still fail after {blocks} tries; letting the session stop"
    rep = check(cfg, data.get("cwd") or cfg["_root"])
    if rep["ok"]:
        if sid in state:
            state.pop(sid)
            write_json(path, state)
        return 0, ""
    state[sid] = blocks + 1
    write_json(path, state)
    return 2, "Gates fail, fix before finishing:\n" + gates.render(rep)


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


def main(event, raw, load=_load, **deps):
    """(exit code, message for stderr). deps are passed to the event handler (fakes in tests)."""
    try:
        data = json.loads(raw or "{}")
        handler = EVENTS.get(event)
        if handler is None:
            return 0, ""
        cfg = load(data.get("cwd"))
        if cfg is None:
            return 0, ""
        return handler(cfg, data, **deps)
    except Exception as e:  # a hook must never break the session
        return 0, f"agent-kit hook {event}: skipped ({type(e).__name__}: {str(e)[:200]})"


def install(path):
    write_json(path, merge(read_json(path, {}) or {}))
    log(f"hooks installed in {path}")


def uninstall(path):
    if path.exists():
        write_json(path, unmerge(read_json(path, {}) or {}))
        log(f"agent-kit hooks removed from {path}")
