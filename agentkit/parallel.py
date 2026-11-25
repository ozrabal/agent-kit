"""Running independent tickets at the same time: which ones run together, the per-ticket lock that
keeps two loops off one ticket, and the git lock around writes to the main repo's .git/config.
Pure or file-only functions; the loop (loop.py) spawns the processes."""
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

CHILD_ENV = "AGENT_KIT_PARALLEL"  # "1" in a child run: the parent already reconciled and prepared it


def alive(pid):
    """True when a process with this pid exists (also one of another user)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) and isinstance(d.get("pid"), int) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


STALE_SECS = 600  # a lock with no heartbeat for this long is stale even if its pid lives (pid reused)
BEAT_SECS = 60  # how often the holder rewrites its ts (parallel.hold)


def _live(d, alive, now):
    return bool(d) and alive(d["pid"]) and now() - (d.get("ts") or 0) < STALE_SECS


def holder(path, alive=alive, now=time.time):
    """The live run holding the lock ({pid, ts of its last heartbeat}), or None (no lock, or a stale one)."""
    d = _read(path)
    return d if _live(d, alive, now) else None


def take(path, pid=None, alive=alive, now=time.time):
    """Take the lock file (O_EXCL). A lock whose pid is gone, or with no heartbeat for STALE_SECS, is stale:
    removed and retaken. Breaking a stale lock happens under <lock>.guard, so two runs that both saw the same
    stale lock cannot both win. Returns {"taken", "holder": the live holder when not taken, "stale": the
    removed lock (its contents, {} if unreadable) or None}."""
    path, pid = Path(path), pid or os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    with flock(str(path) + ".guard"):
        stale = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            d = _read(path)
            if _live(d, alive, now):
                return {"taken": False, "holder": d, "stale": None}
            stale = d
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": pid, "ts": int(now())}))
        return {"taken": True, "holder": None, "stale": stale}


def beat(path, pid=None, now=time.time):
    """The holder's heartbeat: rewrite ts (atomically) if this pid holds the lock."""
    pid = pid or os.getpid()
    if _read(path).get("pid") == pid:
        tmp = Path(f"{path}.{pid}.tmp")
        tmp.write_text(json.dumps({"pid": pid, "ts": int(now())}))
        os.replace(tmp, path)


@contextlib.contextmanager
def hold(path, every=BEAT_SECS):
    """Beat for the lock in a background thread while the block runs."""
    stop = threading.Event()
    t = threading.Thread(target=lambda: [beat(path) for _ in iter(lambda: stop.wait(every), True)], daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def release(path, pid=None):
    """Remove the lock if this process holds it."""
    if _read(path).get("pid") == (pid or os.getpid()):
        Path(path).unlink(missing_ok=True)


@contextlib.contextmanager
def flock(path):
    """An exclusive lock held for a short block (released by the OS when the process dies)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def slots(parallel, max_open, open_prs):
    """How many tickets may start now: loop.parallel, but never more PRs than merge.max_open_prs."""
    return max(0, min(parallel, max_open - open_prs))


def select(candidates, n, locked, ready):
    """Up to n tickets to run together, in number order: {"picked": [numbers], "left": [{number, why}]}.
    An unblocked candidate never blocks another one (a blocker that is not done leaves it waiting), so the
    picked set is independent. locked: {number: holder}. ready(c): refine; asked only while a slot is free."""
    picked, left = [], []
    for c in candidates:
        num = c["number"]
        if c["unmet"]:
            why = "waits on " + ", ".join(f"#{u}" for u in c["unmet"])
        elif num in locked:
            why = f"locked by pid {locked[num]['pid']} (another agent-kit run)"
        elif len(picked) >= n:
            why = f"no free slot ({len(picked)} of {n} used)"
        elif not ready(c):
            why = "not ready (refine)"
        else:
            picked.append(num)
            continue
        left.append({"number": num, "why": why})
    return {"picked": picked, "left": left}


def run_children(cmds, cwd, emit=None):
    """Run {ticket: command} at the same time; print each output line as `[agent-kit #<ticket>] ...` as it comes.
    Waits for all. Returns {ticket: {"pid", "exit", "secs"}}. Ctrl-C reaches the children too (same process
    group): they get up to 30s to stop, then they are killed, and KeyboardInterrupt goes on to the caller."""
    emit = emit or (lambda line: print(line, file=sys.stderr, flush=True))
    env = {**os.environ, CHILD_ENV: "1", "PYTHONUNBUFFERED": "1"}
    say, res, threads, procs = threading.Lock(), {}, [], {}

    def pump(n, p, t0):
        for line in p.stdout:
            line = line.rstrip("\n")
            with say:
                emit(f"[agent-kit #{n}] {line.removeprefix('[agent-kit] ')}")
        p.wait()
        res[n] = {"pid": p.pid, "exit": p.returncode, "secs": round(time.time() - t0, 1)}

    for n, cmd in cmds.items():
        procs[n] = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threads.append(threading.Thread(target=pump, args=(n, procs[n], time.time()), daemon=True))
        threads[-1].start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for p in procs.values():
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        raise
    return res


def worst(results):
    """The batch's exit code: the highest child exit code (0 when all succeeded); a child killed by a signal
    (negative returncode) counts as 128 + signal, like a shell."""
    return max((r["exit"] if r["exit"] >= 0 else 128 - r["exit"] for r in results.values()), default=0)
