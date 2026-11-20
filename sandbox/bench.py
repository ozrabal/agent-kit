#!/usr/bin/env python3
"""Sandbox benchmark: run a fixed set of tickets and save cost, tokens and rounds, to compare changes.

    python3 sandbox/bench.py                      # reset the sandbox, run tickets 1,4,5, save the report
    python3 sandbox/bench.py --tickets 1,4        # other tickets (1-based index in issues.json)
    python3 sandbox/bench.py --compare a.json b.json

Costs money: about $1-2 per ticket with the sandbox budgets. Results go to sandbox/bench/
(<date>-<git short sha>.json, not committed). Tickets are matched by title when comparing,
because a reset gives them new issue numbers.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from agentkit.report import METRICS, MONEY  # noqa: E402

OUT = HERE / "bench"
DEFAULT_TICKETS = "1,4,5"  # normal implementation probes; 2 and 3 wait for merges, 6 is not ready


def _delta(a, b):
    if a is None or b is None:
        return (a, b, None)
    return (a, b, round(b - a, 4))


def compare(before, after):
    """One entry per ticket title (plus "total"), each metric as (before, after, change)."""
    a = {r["title"]: r for r in before["rows"]}
    b = {r["title"]: r for r in after["rows"]}
    titles = list(a) + [t for t in b if t not in a]
    rows = [{"title": t, **{m: _delta(a.get(t, {}).get(m), b.get(t, {}).get(m)) for m in METRICS}} for t in titles]
    rows.append({"title": "total", **{m: _delta(before["total"].get(m), after["total"].get(m)) for m in METRICS}})
    return rows


def _num(v, m):
    return "-" if v is None else (f"{v:.2f}" if m in MONEY else str(v))


def format_compare(rows):
    lines = []
    for r in rows:
        lines.append(r["title"])
        for m in METRICS:
            a, b, d = r[m]
            change = "" if d is None else f" ({'+' if d > 0 else ''}{_num(d, m)})"
            lines.append(f"  {m:<13} {_num(a, m)} -> {_num(b, m)}{change}")
    return "\n".join(lines)


def _run(cmd, cwd=None, must=False):
    """Run and echo a command. must=True exits with its error output when it fails."""
    print("$ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if must and r.returncode != 0:
        sys.exit(f"failed ({r.returncode}): {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


def run_bench(tickets):
    sys.path.insert(0, str(HERE))
    from reset import sandbox_dir  # noqa: E402

    titles = [json.loads((HERE / "issues.json").read_text())[i - 1]["title"] for i in tickets]
    _run([sys.executable, str(HERE / "reset.py")], must=True)
    local = sandbox_dir(None) / "agent-kit-sandbox"
    _run(["agent-kit", "kb", "update", "--no-llm"], cwd=local, must=True)
    listed = _run(["gh", "issue", "list", "--state", "open", "--json", "number,title", "--limit", "100"],
                  cwd=local, must=True)
    numbers = {i["title"]: i["number"] for i in json.loads(listed.stdout)}
    start = int(time.time())
    for t in titles:
        if t not in numbers:
            sys.exit(f"ticket not found in the sandbox: {t}")
        r = _run(["agent-kit", "run", "--ticket", str(numbers[t])], cwd=local)
        print(f"  {t}: exit {r.returncode}")  # a failed ticket is a result too; keep going
    rep = _run(["agent-kit", "report", "--json", "--since", str(start)], cwd=local, must=True)
    sha = _run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE).stdout.strip()
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{time.strftime('%Y-%m-%d')}-{sha or 'nogit'}.json"
    path.write_text(rep.stdout)
    print(f"saved {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickets", default=DEFAULT_TICKETS, help="comma-separated 1-based indexes in issues.json")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two saved results")
    a = ap.parse_args()
    if a.compare:
        before, after = (json.loads(Path(p).read_text()) for p in a.compare)
        print(format_compare(compare(before, after)))
        return
    run_bench([int(x) for x in a.tickets.split(",")])


if __name__ == "__main__":
    main()
