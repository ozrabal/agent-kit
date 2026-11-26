"""Claude Code hooks: `agent-kit hook <event>` reads the hook's stdin JSON and runs gates inside
the session, so Claude fixes errors while it still has the context. No LLM. The hook log also gets
a `gates` row when Claude runs `agent-kit gates` itself during a loop run (record_gates).

Exit 2 blocks (Claude sees the message); anything else is exit 0. A hook never crashes a session:
an internal error is exit 0 with a short note."""
import fnmatch
import json
import os
import re
import shlex
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, gates, kb
from .util import append_jsonl, log, main_root, read_json, read_jsonl, repo_root, sh, write_json

# Outcomes of a hook call, as logged in .agent/runs/hooks.jsonl.
PASS, BLOCK, SKIP, GIVE_UP, ERROR = "pass", "block", "skip", "give_up", "error"


def _load(cwd):
    try:
        cfg = config.load(cwd)
    except RuntimeError:  # not a git repo
        return None
    if not cfg["_configured"] and config.config_path(cfg["_root"]).exists():
        raise ValueError(f"cannot read {config.config_path(cfg['_root'])} (not a JSON object?)")
    return cfg if cfg["_configured"] else None


def _repo(cwd):
    """The checkout Claude works in (a worktree in the loop); cwd itself outside git."""
    try:
        return repo_root(cwd)
    except (RuntimeError, OSError):
        return Path(cwd)


def _agent_root(cwd):
    """The main checkout for cwd when it has an .agent/ folder (used to log a config that fails to load)."""
    try:
        root = main_root(cwd)
    except (RuntimeError, OSError):
        return None
    return root if (root / ".agent").is_dir() else None


def _outcome(outcome, code=0, msg="", out="", **row):
    """What a handler returns: (exit code, stderr message, fields for the hook log row, stdout)."""
    return code, msg, {"outcome": outcome, **row}, out


def _gates_row(report):
    return [{k: r[k] for k in ("name", "ok", "secs")} for r in report["results"]]


def post_edit(cfg, data, run=sh):
    """PostToolUse on Edit|Write|MultiEdit: run the file_gates whose `match` glob fits the edited file."""
    path = (data.get("tool_input") or {}).get("file_path")
    if not path:
        return _outcome(SKIP, note="no file_path in tool_input")
    if not cfg["file_gates"]:
        return _outcome(SKIP, file=path, note="no file_gates")
    root = _repo(data.get("cwd") or cfg["_root"]).resolve()
    try:
        rel = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return _outcome(SKIP, file=path, note="outside the repo")
    env, results = gates.env(cfg), []
    for g in cfg["file_gates"]:
        if fnmatch.fnmatch(rel, g["match"]):
            g = {**g, "name": f"{g['name']} {rel}", "cmd": g["cmd"].replace("{file}", shlex.quote(rel))}
            results.append(gates.run_one(g, root, env, run=run, timeout=cfg["hooks"]["file_gate_timeout"]))
    if not results:
        return _outcome(SKIP, file=rel, note="no file_gates match")
    rep = {"results": results}
    failed = {"results": [r for r in results if not r["ok"]]}
    if not failed["results"]:
        return _outcome(PASS, file=rel, gates=_gates_row(rep))
    text = gates.render(failed)
    return _outcome(BLOCK, 2, text, file=rel, gates=_gates_row(rep), note=text)


def _check(cfg, cwd):
    gates.autofix(cfg, cwd)
    return gates.run(cfg, cwd)


def stop(cfg, data, check=_check):
    """Stop: block while gates fail, but at most hooks.stop_max_blocks times in a row per session."""
    h = cfg["hooks"]
    if not h["stop_gate"]:
        return _outcome(SKIP, note="hooks.stop_gate is off")
    path, sid = config.hook_state_path(cfg), data.get("session_id") or "-"
    state = read_json(path, {}) or {}
    blocks = state.get(sid, 0) if data.get("stop_hook_active") else 0
    if blocks >= h["stop_max_blocks"]:  # kept until a fresh stop (stop_hook_active false) resets it
        return _outcome(GIVE_UP, blocks=blocks, note=f"gates still fail after {blocks} blocks; letting the session stop")
    rep = check(cfg, data.get("cwd") or cfg["_root"])
    if rep["ok"]:
        if sid in state:
            state.pop(sid)
            write_json(path, state)
        return _outcome(PASS, gates=_gates_row(rep), blocks=0)
    state[sid] = blocks + 1
    write_json(path, state)
    text = gates.render(rep)
    return _outcome(BLOCK, 2, "Gates fail, fix before finishing:\n" + text,
                    gates=_gates_row(rep), blocks=blocks + 1, note=text)


def active_run(cfg, cwd):
    """state.json of the loop run whose worktree is cwd's checkout and that is still working, or None."""
    here = _repo(cwd).resolve()
    for f in config.runs_dir(cfg).glob("*/state.json"):
        s = read_json(f, {}) or {}
        if s.get("ws") and Path(s["ws"]).resolve() == here and str(s.get("status") or "").startswith((config.IMPLEMENTING, config.FIXING_REVIEW)):
            return s
    return None


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def guard(cfg, data):
    """PreToolUse: keep edits in the run's worktree and off protected paths; deny risky Bash."""
    g, cwd = cfg["guard"], _cwd(cfg, data)
    tool, inp = data.get("tool_name"), data.get("tool_input") or {}
    run = active_run(cfg, cwd)
    if run is None and not g["always"]:
        return _outcome(SKIP, tool=tool, note="no active loop run")
    root = Path(run["ws"] if run else _repo(cwd)).resolve()
    if tool in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        if not path:
            return _outcome(SKIP, tool=tool, note="no file path in tool_input")
        try:
            rel = (Path(cwd) / path).resolve().relative_to(root).as_posix()
        except ValueError:
            return _deny(tool, f"{path} is outside the worktree", f"edit files inside {root}", file=path)
        hit = next((p for p in g["protected"] if fnmatch.fnmatch(rel, p)), None)
        if hit:
            return _deny(tool, f"{rel} is protected ({hit})",
                         f"edit project files inside {root} except {', '.join(g['protected'])}", file=path)
        return _outcome(PASS, tool=tool, file=path)
    if tool == "Bash":
        cmd = inp.get("command") or ""
        hit = next((p for p in g["deny_bash"] if re.search(p, cmd)), None)
        if hit:
            return _deny(tool, f"this command matches guard.deny_bash ({hit})",
                         "read-only git, `agent-kit gates`, and the project's own commands. The driver "
                         "commits, pushes and opens the PR", command=cmd[:300])
        return _outcome(PASS, tool=tool, command=cmd[:300])
    return _outcome(SKIP, tool=tool, note="not a guarded tool")


def _deny(tool, why, allowed, **row):
    msg = f"agent-kit guard: blocked {tool}: {why}. Allowed instead: {allowed}."
    return _outcome(BLOCK, 2, msg, tool=tool, note=msg, **row)


HINT = ("Before exploring the repo for a task, run `agent-kit kb context \"<task>\"` (or `#<issue>`): "
        "likely files, past tickets, gotchas and the full rules for those files.")


def _cwd(cfg, data):
    return data.get("cwd") or cfg["_root"]


def _digest_and_hint(cfg, cwd):
    """For sessions without a context pack: the rules digest (if any) and how to get a pack."""
    f = kb.kdir(cfg, _repo(cwd)) / "rules-digest.md"
    digest = f.read_text().strip() if f.exists() else ""
    return "\n\n".join(x for x in (digest, HINT) if x), "digest" if digest else "hint only"


def session_start(cfg, data):
    """SessionStart (stdout goes into Claude's context): the rules digest and a kb hint, except in a loop
    run, whose prompt already has the context pack."""
    cwd = _cwd(cfg, data)
    if active_run(cfg, cwd):
        return _outcome(SKIP, note="loop run: the prompt has the pack")
    text, note = _digest_and_hint(cfg, cwd)
    return _outcome(PASS, out=text, note=note)


def subagent_start(cfg, data):
    """SubagentStart: give the sub-agent the run's context pack (context.md), or the digest outside a run.
    It can only add context (JSON additionalContext), not block."""
    cwd = _cwd(cfg, data)
    run = active_run(cfg, cwd)
    pack = config.runs_dir(cfg) / str(run["number"]) / "context.md" if run else None
    if pack and pack.exists():
        text, note = pack.read_text(), f"context pack of #{run['number']}"
    else:
        text, note = _digest_and_hint(cfg, cwd)
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}})
    return _outcome(PASS, out=out, agent=data.get("agent_type"), note=note)


EVENTS = {"post-edit": post_edit, "stop": stop, "guard": guard, "session-start": session_start,
          "subagent-start": subagent_start}

# What `agent-kit hooks install` writes into .claude/settings.json: {Claude event: (matcher, our event)}.
SETTINGS = {"PostToolUse": ("Edit|Write|MultiEdit", "post-edit"), "Stop": (None, "stop"),
            "PreToolUse": ("|".join(EDIT_TOOLS) + "|Bash", "guard"), "SessionStart": (None, "session-start"),
            "SubagentStart": (None, "subagent-start")}


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


def missing(settings):
    """Claude events whose agent-kit hook is not in settings."""
    hk = (settings or {}).get("hooks") or {}
    return sorted(event for event, (_, ours) in SETTINGS.items()
                  if not any(h.get("command") == command(ours) for g in hk.get(event, []) for h in g.get("hooks", [])))


def installed(settings):
    return not missing(settings)


def render_row(row, cwd=None):
    """One readable line for a hook log row, for `agent-kit log` and the live run output."""
    f = _relative(row.get("file") or "", cwd)
    if row.get("command"):
        f = f"`{row['command'][:80]}`"
    if row.get("tool"):
        f = f"{row['tool']} {f}".strip()
    head = f"hook {row.get('event')}{' ' + f if f else ''}: {str(row.get('outcome')).upper()}"
    if row.get("event") == "stop" and row.get("outcome") in (BLOCK, GIVE_UP):
        head += f" #{row.get('blocks')}"
    parts = [head] + [f"{g['name']}={'ok' if g['ok'] else 'FAIL'}" for g in row.get("gates") or []]
    line = " ".join(parts) + f" ({row.get('secs')}s)"
    note = (row.get("note") or "").strip().splitlines()
    return line + (f" | {note[0]}" if note else "")


LOG_NAME = "hooks.jsonl"


def log_path(cfg):
    return config.runs_dir(cfg) / LOG_NAME


def _inside(path, folder):
    path, folder = str(path or ""), str(folder or "").rstrip("/")
    return bool(path and folder) and (path == folder or path.startswith(folder + "/"))


def _relative(path, folder):
    return path[len(str(folder).rstrip("/")) + 1:] if folder and _inside(path, folder) and path != str(folder) else path


def rows_for(rows, state, step=None):
    """Hook rows of one ticket run: by the Claude session ids it recorded (state["sessions"]), so tickets
    sharing a folder stay apart; runs from before sessions were recorded match by worktree folder. A row
    without a session id (an `agent-kit gates` call that got none from Claude) matches by run key, for no step."""
    sessions = [s for s in state.get("sessions") or [] if step is None or s["step"] == step]
    if sessions or state.get("sessions"):
        ids = {s["id"] for s in sessions}
        key = state.get("key") if step is None else None
        return [r for r in rows if r.get("session") in ids or (key and not r.get("session") and r.get("key") == key)]
    return [] if step else [r for r in rows if _inside(r.get("cwd"), state.get("ws"))]


def read_rows(cfg):
    return read_jsonl(log_path(cfg))


@contextmanager
def watch(cfg, ws, step, every=1.0):
    """While the block runs, print each new hook row from a session in ws as `<step> | hook ...`.
    Reads bytes, so offsets stay right for any text; a read error is reported once and ends the watch."""
    path, done = log_path(cfg), threading.Event()
    start = path.stat().st_size if path.exists() else 0

    def read_new(pos):
        try:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
        except FileNotFoundError:
            return pos, []
        whole = chunk[: chunk.rfind(b"\n") + 1]  # only complete lines
        return pos + len(whole), whole.decode("utf-8", "replace").splitlines()

    def tail():
        pos = start
        try:
            while True:
                last = done.wait(every)
                pos, lines = read_new(pos)
                for line in lines:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _inside(row.get("cwd"), ws):
                        log(f"  {step} | {render_row(row, cwd=ws)}")
                if last:  # one read after the session ended: its hooks have all finished by then
                    return
        except Exception as e:
            log(f"  {step} | live hook output stopped ({type(e).__name__}: {e}); see `agent-kit log`")

    t = threading.Thread(target=tail, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join()


def _log(cfg, event, data, row, t0):
    row = {"ts": int(t0), "event": event, "session": data.get("session_id"), "cwd": data.get("cwd"),
           **row, "secs": round(time.time() - t0, 2)}
    if row.get("note"):
        row["note"] = row["note"][: cfg["hooks"]["note_chars"]]
    append_jsonl(log_path(cfg), row)


def record_gates(cfg, cwd, report, t0, env=None):
    """`agent-kit gates` run inside an active loop run's worktree (Claude checking its own work): one `gates`
    row in hooks.jsonl, BLOCK with the failures as note, else PASS. Outside a run nothing is logged.
    The row carries Claude's session id (CLAUDE_CODE_SESSION_ID) and the run key, so rows_for finds it.
    Never raises, like a hook: returns a message for stderr when the row could not be written, else None."""
    try:
        state = active_run(cfg, cwd)
        if state is None:
            return None
        failed = {"results": [r for r in report["results"] if not r["ok"]]}
        row = _outcome(BLOCK, note=gates.render(failed)) if failed["results"] else _outcome(PASS)
        env = os.environ if env is None else env
        data = {"session_id": env.get("CLAUDE_CODE_SESSION_ID"), "cwd": str(cwd)}
        _log(cfg, "gates", data, {**row[2], "key": state.get("key"), "gates": _gates_row(report)}, t0)
    except Exception as e:  # the gate output matters more than the log row
        return f"agent-kit gates: could not log this gates run ({type(e).__name__}: {e})"
    return None


def main(event, raw, load=_load, agent_root=_agent_root, **deps):
    """(exit code, message for stderr, text for stdout). Every call in a repo with .agent/ is logged to
    .agent/runs/hooks.jsonl, also when the config fails to load. deps go to the event handler (fakes in tests)."""
    t0, cfg, data = time.time(), None, {}
    try:
        data = json.loads(raw or "{}")
        cfg = load(data.get("cwd"))
        if cfg is None:  # not an agent-kit repo: nothing to do, nothing to log
            return 0, "", ""
        handler = EVENTS.get(event)
        if handler is None:
            raise ValueError(f"unknown event {event!r} (known: {', '.join(EVENTS)})")
        code, msg, row, out = handler(cfg, data, **deps)
    except Exception as e:  # a hook must never break the session
        code, msg, row, out = _outcome(ERROR, note=f"{type(e).__name__}: {str(e)[:300]}")
        if cfg is None:
            root = agent_root(data.get("cwd")) if isinstance(data, dict) else None
            cfg = {"_root": str(root), "hooks": config.DEFAULTS["hooks"]} if root else None
    if not msg and row["outcome"] in (ERROR, GIVE_UP):
        msg = f"agent-kit hook {event}: {row['note']}"
    if cfg is not None:
        try:
            _log(cfg, event, data if isinstance(data, dict) else {}, row, t0)
        except Exception as e:  # logging must not break the session either
            msg = (msg + "\n" if msg else "") + f"agent-kit hook {event}: could not write the hook log ({e})"
    return code, msg, out


def install(path):
    write_json(path, merge(read_json(path, {}) or {}))
    log(f"hooks installed in {path}")


def uninstall(path):
    if path.exists():
        write_json(path, unmerge(read_json(path, {}) or {}))
        log(f"agent-kit hooks removed from {path}")
