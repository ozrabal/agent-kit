"""Parallel tickets: the per-ticket lock, the git lock and which tickets run together.
Run: python3 -m unittest tests.test_parallel"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentkit import parallel  # noqa: E402
from agentkit.config import DEFAULTS  # noqa: E402
from agentkit.loop import Loop, Run  # noqa: E402


def cand(n, unmet=()):
    return {"number": n, "key": f"#{n}", "title": f"t{n}", "unmet": list(unmet)}


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "153" / "lock"

    def test_take_and_release(self):
        got = parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        self.assertEqual(got, {"taken": True, "holder": None, "stale": None})
        self.assertEqual(json.loads(self.path.read_text()), {"pid": 111, "ts": 1000})
        parallel.release(self.path, pid=111)
        self.assertFalse(self.path.exists())

    def test_live_holder_blocks(self):
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        got = parallel.take(self.path, pid=222, alive=lambda p: True, now=lambda: 1001)
        self.assertEqual(got, {"taken": False, "holder": {"pid": 111, "ts": 1000}, "stale": None})

    def test_dead_holder_is_stale_and_replaced(self):
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        got = parallel.take(self.path, pid=222, alive=lambda p: p != 111, now=lambda: 1001)
        self.assertEqual(got, {"taken": True, "holder": None, "stale": {"pid": 111, "ts": 1000}})
        self.assertEqual(json.loads(self.path.read_text())["pid"], 222)

    def test_unreadable_lock_is_stale(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("garbage")
        got = parallel.take(self.path, pid=222, alive=lambda p: True, now=lambda: 1)
        self.assertEqual((got["taken"], got["stale"]), (True, {}))

    def test_release_leaves_someone_elses_lock(self):
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        parallel.release(self.path, pid=222)
        self.assertTrue(self.path.exists())

    def test_no_heartbeat_is_stale_even_if_the_pid_lives(self):
        """pid reuse: the pid now belongs to another process, but no agent-kit run beats for the lock any more."""
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        got = parallel.take(self.path, pid=222, alive=lambda p: True, now=lambda: 1000 + parallel.STALE_SECS + 1)
        self.assertEqual((got["taken"], got["stale"]), (True, {"pid": 111, "ts": 1000}))

    def test_heartbeat_keeps_it_live(self):
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        parallel.beat(self.path, pid=111, now=lambda: 1500)
        parallel.beat(self.path, pid=222, now=lambda: 1900)  # not the holder: no effect
        self.assertEqual(json.loads(self.path.read_text()), {"pid": 111, "ts": 1500})
        got = parallel.take(self.path, pid=222, alive=lambda p: True, now=lambda: 1500 + parallel.STALE_SECS - 1)
        self.assertFalse(got["taken"])

    def test_hold_beats_in_the_background(self):
        parallel.take(self.path)
        with parallel.hold(self.path, every=0.05):
            time.sleep(0.2)
            self.assertEqual(json.loads(self.path.read_text())["pid"], os.getpid())
        self.assertTrue(self.path.exists(), "hold only beats; release is separate")

    def test_holder(self):
        self.assertIsNone(parallel.holder(self.path, alive=lambda p: True))
        parallel.take(self.path, pid=111, alive=lambda p: True, now=lambda: 1000)
        self.assertEqual(parallel.holder(self.path, alive=lambda p: True, now=lambda: 1001), {"pid": 111, "ts": 1000})
        self.assertIsNone(parallel.holder(self.path, alive=lambda p: False, now=lambda: 1001))

    def test_alive_real_pids(self):
        self.assertTrue(parallel.alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(parallel.alive(p.pid))


class GitLock(unittest.TestCase):
    def test_serializes_two_processes(self):
        """A child holds the lock for 0.5s; the parent's critical section starts only after the child's ends."""
        with tempfile.TemporaryDirectory() as tmp:
            path, marks = Path(tmp) / "git.lock", Path(tmp) / "marks"
            code = (f"import sys,time; sys.path.insert(0,{str(ROOT)!r}); from agentkit import parallel\n"
                    f"with parallel.flock({str(path)!r}):\n"
                    f"    open({str(marks)!r},'a').write('child-in\\n'); print('in', flush=True); time.sleep(0.5)\n"
                    f"    open({str(marks)!r},'a').write('child-out\\n')\n")
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            child.stdout.readline()  # the child holds the lock now
            with parallel.flock(path):
                with open(marks, "a") as f:
                    f.write("parent\n")
            child.wait()
            self.assertEqual(marks.read_text().split(), ["child-in", "child-out", "parent"])


class Select(unittest.TestCase):
    def test_slots(self):
        self.assertEqual(parallel.slots(parallel=2, max_open=3, open_prs=0), 2)
        self.assertEqual(parallel.slots(parallel=4, max_open=3, open_prs=2), 1)
        self.assertEqual(parallel.slots(parallel=2, max_open=1, open_prs=1), 0)

    def test_picks_unblocked_up_to_slots_with_reasons(self):
        cands = [cand(153), cand(154, [153]), cand(156), cand(157), cand(158)]
        got = parallel.select(cands, 2, locked={}, ready=lambda c: True)
        self.assertEqual(got, {"picked": [153, 156], "left": [
            {"number": 154, "why": "waits on #153"},
            {"number": 157, "why": "no free slot (2 of 2 used)"},
            {"number": 158, "why": "no free slot (2 of 2 used)"}]})

    def test_locked_and_not_ready_are_left_out(self):
        cands = [cand(153), cand(156), cand(157), cand(158)]
        got = parallel.select(cands, 2, locked={153: {"pid": 99, "ts": 1}}, ready=lambda c: c["number"] != 156)
        self.assertEqual(got["picked"], [157, 158])
        self.assertEqual(got["left"], [{"number": 153, "why": "locked by pid 99 (another agent-kit run)"},
                                       {"number": 156, "why": "not ready (refine)"}])

    def test_ready_is_asked_only_until_slots_are_full(self):
        """ready() may cost an LLM call (refine auto): never call it for tickets that would not run anyway."""
        asked = []
        parallel.select([cand(1), cand(2), cand(3)], 1, locked={}, ready=lambda c: asked.append(c["number"]) or True)
        self.assertEqual(asked, [1])


class Children(unittest.TestCase):
    """parallel.run_children: real child processes, output prefixed per ticket, exit codes collected."""

    def test_prefixed_output_and_exit_codes_run_at_once(self):
        lines = []
        code = "import sys,time; print('[agent-kit] start', file=sys.stderr, flush=True); time.sleep(0.6); print('plain'); sys.exit({})"
        t0 = time.time()
        res = parallel.run_children({153: [sys.executable, "-c", code.format(0)],
                                     156: [sys.executable, "-c", code.format(2)]}, cwd=".", emit=lines.append)
        self.assertLess(time.time() - t0, 1.1)  # 0.6s each, at the same time
        self.assertEqual({n: r["exit"] for n, r in res.items()}, {153: 0, 156: 2})
        self.assertTrue(all(r["pid"] and r["secs"] >= 0.6 for r in res.values()))
        self.assertEqual(sorted(lines), ["[agent-kit #153] plain", "[agent-kit #153] start",
                                         "[agent-kit #156] plain", "[agent-kit #156] start"])

    def test_child_env_marks_it(self):
        lines = []
        parallel.run_children({1: [sys.executable, "-c", f"import os; print(os.environ.get('{parallel.CHILD_ENV}'))"]},
                              cwd=".", emit=lines.append)
        self.assertEqual(lines, ["[agent-kit #1] 1"])

    def test_worst_exit(self):
        self.assertEqual(parallel.worst({1: {"exit": 0}, 2: {"exit": 2}, 3: {"exit": 130}}), 130)
        self.assertEqual(parallel.worst({}), 0)
        self.assertEqual(parallel.worst({1: {"exit": 0}, 2: {"exit": -9}}), 137)  # killed by a signal


BODY = "## Acceptance criteria\n\n- works\n"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class BatchTracker:
    def __init__(self):
        self.iss = {n: {"number": n, "title": f"Ticket {n}", "body": BODY, "labels": ["ready-for-agent"], "state": "OPEN"}
                    for n in (153, 154, 156)}
        self.labels = []

    def issues(self, refresh=False):
        return self.iss

    def get(self, n):
        return self.iss[n]

    def key(self, i):
        return f"#{i['number']}"

    def blockers(self, i):
        return [153] if i["number"] == 154 else []

    def candidates(self):
        return [{**i, "key": f"#{n}", "blocked_by": self.blockers(i), "unmet": self.blockers(i)}
                for n, i in sorted(self.iss.items()) if i["state"] == "OPEN" and "ready-for-agent" in i["labels"]]

    def set_status(self, n, old, new):
        self.labels.append((n, old, new))


class RepoCase(unittest.TestCase):
    """A temp repo with a bare origin, loop.parallel 2; children are injected commands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(root / "origin.git"))
        (self.repo / ".gitignore").write_text(".agent/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "seed")
        git(self.repo, "push", "-q", "origin", "main")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(_root=str(self.repo), _configured=True)
        self.cfg["workspace"]["dir"] = str(root / "wt")
        self.cfg["loop"]["parallel"] = 2
        self.cfg["merge"]["max_open_prs"] = 2
        self.tr = BatchTracker()
        self.started = []

    def child(self, n):
        self.started.append(n)
        return [sys.executable, "-c", f"import time; print('working on {n}'); time.sleep(0.3)"]

    def loop(self):
        return Loop(self.cfg, claude=None, tracker=self.tr, child_cmd=self.child)


class LoopBatch(RepoCase):
    """Loop.run with loop.parallel 2."""

    def test_independent_tickets_run_together_in_own_worktrees(self):
        self.assertEqual(self.loop().run(max_tickets=2), 0)
        self.assertEqual(self.started, [153, 156])
        for n in (153, 156):
            st = Run(self.cfg, n)
            self.assertTrue(Path(st["ws"]).is_dir(), "worktree made by the parent before the child starts")
            p = st["parallel"]
            self.assertEqual((p["batch"], p["slots"], p["exit"]), ([153, 156], 2, 0))
            self.assertEqual(p["left"], [{"number": 154, "why": "waits on #153"}])
            self.assertEqual(st["status"], "queued")
        self.assertNotEqual(Run(self.cfg, 153)["ws"], Run(self.cfg, 156)["ws"])
        self.assertFalse(Run(self.cfg, 154).file.exists())

    def test_one_slot_runs_inline(self):
        """max_open_prs 1: no children; the sequential path runs (do_ticket in this process)."""
        self.cfg["merge"]["max_open_prs"] = 1
        ran = []
        lp = self.loop()
        lp.do_ticket = lambda issue, dry=False: ran.append(issue["number"]) or True
        lp.run(max_tickets=1)
        self.assertEqual((ran, self.started), ([153], []))

    def test_dry_run_never_spawns(self):
        lp = self.loop()
        lp.do_ticket = lambda issue, dry=False: True
        self.assertEqual(lp.run(dry=True), 0)
        self.assertEqual(self.started, [])

    def test_single_pick_is_recorded_and_runs_inline(self):
        self.tr.iss[156]["state"] = "CLOSED"
        ran = []
        lp = self.loop()
        def do(issue, dry=False):  # a real run labels the issue in-progress: no longer a candidate
            ran.append(issue["number"])
            self.tr.iss[issue["number"]]["labels"] = []
            return True
        lp.do_ticket = do
        self.assertEqual(lp.run(), 0)
        self.assertEqual((ran, self.started), ([153], []))
        p = Run(self.cfg, 153)["parallel"]
        self.assertEqual((p["batch"], p["slots"]), ([153], 2))

    def test_worktree_failure_drops_that_ticket_only(self):
        self.cfg["workspace"]["setup"] = ['case "$PWD" in *156-*) echo boom; exit 1;; esac']
        self.assertEqual(self.loop().run(max_tickets=2), 2)
        self.assertEqual(self.started, [153])
        self.assertEqual(Run(self.cfg, 153)["parallel"]["exit"], 0)
        st = Run(self.cfg, 156)
        self.assertIsNone(st["status"])
        self.assertIn("boom", st["parallel"]["error"])

    def test_failed_child_gives_the_worst_exit(self):
        self.child = lambda n: [sys.executable, "-c", f"import sys; sys.exit({2 if n == 156 else 0})"]
        self.assertEqual(self.loop().run(max_tickets=2), 2)
        self.assertEqual(Run(self.cfg, 156)["parallel"]["exit"], 2)


class TicketLock(RepoCase):
    """do_ticket takes .agent/runs/<n>/lock: a live holder skips the ticket, a dead one is stale."""

    def claude(self, text, **kw):
        return {"summary": "", "pr_sections": [], "lessons": [], "concerns": []}, "sid"

    def issue(self):
        return {**self.tr.get(153), "key": "#153", "blocked_by": []}

    def test_live_holder_skips(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        self.addCleanup(holder.kill)
        lock = Run(self.cfg, 153).dir / "lock"
        parallel.take(lock, pid=holder.pid)
        lp = Loop(self.cfg, claude=self.claude, tracker=self.tr)
        self.assertFalse(lp.do_ticket(self.issue()))
        st = Run(self.cfg, 153)
        self.assertIsNone(st["status"])
        self.assertEqual([(r["event"], r["pid"]) for r in st["locks"]], [("busy", holder.pid)])
        self.assertEqual(self.tr.labels, [])

    def test_status_shows_the_live_holder(self):
        Run(self.cfg, 156).set(key="#156", status="implementing")
        parallel.take(Run(self.cfg, 156).dir / "lock")  # this test process: alive
        self.addCleanup(parallel.release, Run(self.cfg, 156).dir / "lock")
        lines = Loop(self.cfg, claude=self.claude, tracker=self.tr).status_lines()
        self.assertEqual(lines[:3], ["READY   #153 Ticket 153", "BLOCKED #154 Ticket 154 waits on #153", "READY   #156 Ticket 156"])
        self.assertRegex(lines[3], rf"^RUN     #156 #156 status=implementing pr=- cost=\$0.00 \| locked by pid {os.getpid()} since \d\d:\d\d$")

    def test_status_shows_the_batch(self):
        Run(self.cfg, 153).set(key="#153", status="implementing", parallel={"batch": [153, 156], "slots": 2, "left": []})
        line = Loop(self.cfg, claude=self.claude, tracker=self.tr).status_lines()[3]
        self.assertTrue(line.endswith("| batch #153, #156"), line)

    def test_address_waits_for_the_lock(self):
        """`agent-kit address` and a --wait poll never work on one PR at the same time."""
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        self.addCleanup(holder.kill)
        run = Run(self.cfg, 153)
        run.set(key="#153", status="pr_open", pr="https://x/pull/7")
        parallel.take(run.dir / "lock", pid=holder.pid)
        self.assertFalse(Loop(self.cfg, claude=self.claude, tracker=self.tr)._address_safely(run))
        self.assertEqual(Run(self.cfg, 153)["polls"][-1]["note"], f"skipped: locked by pid {holder.pid}")
        self.assertEqual(Run(self.cfg, 153)["locks"][-1]["event"], "busy")

    def test_stale_lock_is_taken_and_released(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = Run(self.cfg, 153).dir / "lock"
        parallel.take(lock, pid=dead.pid)
        lp = Loop(self.cfg, claude=self.claude, tracker=self.tr)
        self.assertFalse(lp.do_ticket(self.issue()))  # the fake implementer changes nothing
        st = Run(self.cfg, 153)
        self.assertEqual([r["event"] for r in st["locks"]], ["stale", "taken", "released"])
        self.assertEqual(st["locks"][0]["pid"], dead.pid)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
