"""Test-first check: the branch's new or changed tests must fail on the base (red) and pass on
the branch (green, already known from the gates). Plain git and the test gate; no LLM."""
import shutil
import tempfile
import time
from pathlib import Path

from . import gates, kb
from .config import fmt
from .util import out, sh


def test_files(changed, test_paths):
    """The changed paths that are test files. No patterns = no test files."""
    return [p for p in changed if test_paths and kb.matches(p, test_paths)]


def gate(cfg):
    """The gate that runs the tests: loop.tdd.gate by name, else the first gate whose name contains
    "test". None when nothing matches."""
    name = cfg["loop"]["tdd"]["gate"]
    for g in map(gates._norm, cfg["gates"]):
        if g["name"] == name if name else "test" in g["name"]:
            return g
    return None


def check(cfg, ws, upstream, exclude=(), run_one=gates.run_one):
    """Run the branch's added or changed test files on a temporary worktree at upstream.
    exclude: git pathspecs the review leaves out (loop._pathspec_excludes), so "docs only" agrees with it.
    None when loop.tdd is off, else {status, reason, files, gate, secs}; status is one of
    red (fails on base: good), passes_on_base, no_tests, skipped. The worktree is always removed."""
    if not cfg["loop"]["tdd"]["enabled"]:
        return None
    t0 = time.time()

    def res(status, reason="", files=(), g=None):
        return {"status": status, "reason": reason, "files": list(files), "gate": g and g["name"],
                "secs": round(time.time() - t0, 1)}

    g = gate(cfg)
    if not g:
        name = cfg["loop"]["tdd"]["gate"]
        return res("skipped", f"no gate named {name}" if name else "no gate name contains 'test' (set loop.tdd.gate)")
    rng = f"{upstream}...HEAD"
    changed = out(["git", "diff", "--name-only", rng, "--", ".", *exclude], cwd=ws).splitlines()
    files = test_files(out(["git", "diff", "--name-only", "--diff-filter=AMR", rng], cwd=ws).splitlines(),
                       cfg["loop"]["tdd"]["test_paths"])
    if not files:
        if kb.docs_only(changed, cfg["review"]["skip_paths"]):
            return res("skipped", "docs only", g=g)
        return res("no_tests", "no test files added or changed", g=g)
    base = Path(tempfile.mkdtemp(prefix="agent-kit-tdd-")) / "base"
    e = gates.env(cfg)
    try:
        sh(["git", "worktree", "add", "-q", "--detach", str(base), upstream], cwd=ws)
        for cmd in cfg["workspace"]["setup"]:
            r = sh(fmt(cmd, cfg), cwd=base, env=e, check=False, timeout=1800)
            if r.returncode != 0:
                return res("skipped", f"setup failed on base: {cmd} (exit {r.returncode})", files, g)
        for p in files:
            (base / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(ws) / p, base / p)
        r = run_one(g, base, e)
    finally:
        sh(["git", "worktree", "remove", "--force", str(base)], cwd=ws, check=False)
        sh(["git", "worktree", "prune"], cwd=ws, check=False)
        shutil.rmtree(base.parent, ignore_errors=True)
    if r["ok"]:
        return res("passes_on_base", "tests pass without the change", files, g)
    return res("red", "red on base -> green on branch", files, g)


def line(result):
    """The PR's "Tests" line for a check result."""
    st, files = result["status"], result.get("files") or []
    if st == "red":
        return f"Tests: {result['reason']} ({len(files)} test file{'s' * (len(files) != 1)}, gate {result['gate']})"
    if st == "skipped":
        return f"Tests: skipped: {result['reason']}"
    return f"Tests: FAIL: {result['reason']}" + (": " + ", ".join(f"`{p}`" for p in files) if files else "")


def finding(result):
    """A blocking review finding for a check result that does not prove the change, else None."""
    st = (result or {}).get("status")
    if st == "passes_on_base":
        files = result["files"]
        return {"severity": "major", "file": files[0],
                "issue": f"tests pass without the change (gate {result['gate']} on the base): {', '.join(files)}",
                "fix": "make these tests fail on the old code: assert the new behavior"}
    if st == "no_tests":
        return {"severity": "major", "file": "-", "issue": "no tests added",
                "fix": "add tests that fail without the change (one per acceptance criterion)"}
    return None
