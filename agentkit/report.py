"""Per-ticket cost, tokens and rounds from .agent/runs/<n>/ (state.json + cost.jsonl). No LLM.
Used by `agent-kit report` and the sandbox benchmark to compare runs."""
from pathlib import Path

from .util import read_json, read_jsonl

TOKENS = ("in", "cache_r", "out")


def _step(row):
    return (row.get("step") or "").split(":")[0]


def _gates(state):
    rep = state.get("gates")
    return "-" if not rep else ("pass" if rep.get("ok") else "FAIL")


def collect(runs_dir, since=None):
    """{"rows": [one per ticket run], "total": sums}. since: unix ts; older calls are ignored
    and runs with no newer calls are left out."""
    rows = []
    runs_dir = Path(runs_dir)
    dirs = runs_dir.iterdir() if runs_dir.is_dir() else []
    for d in sorted((p for p in dirs if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        calls = [c for c in read_jsonl(d / "cost.jsonl") if since is None or (c.get("ts") or 0) >= since]
        if since is not None and not calls:
            continue
        state = read_json(d / "state.json", {}) or {}
        steps = [_step(c) for c in calls]
        row = {
            "ticket": int(d.name), "key": state.get("key") or f"#{d.name}", "title": state.get("title") or "",
            "status": state.get("status") or "-", "usd": round(sum(c.get("usd") or 0 for c in calls), 4),
            "calls": len(calls), "fix_rounds": steps.count("fix-gates"), "review_rounds": steps.count("review"),
            "gates": _gates(state),
        }
        row.update({t: sum(c.get(t) or 0 for c in calls) for t in TOKENS})
        rows.append(row)
    sums = ("usd", "calls", "fix_rounds", "review_rounds", *TOKENS)
    total = {k: round(sum(r[k] for r in rows), 4) for k in sums}
    return {"rows": rows, "total": total}


COLUMNS = [("ticket", "key"), ("status", "status"), ("usd", "usd"), ("calls", "calls"), ("fix", "fix_rounds"),
           ("review", "review_rounds"), ("gates", "gates"), *((t, t) for t in TOKENS)]


def render(rep):
    if not rep["rows"]:
        return "no runs"
    def cells(row):
        return [f"{row[k]:.2f}" if k == "usd" else str(row.get(k, "")) for _, k in COLUMNS]
    table = [[h for h, _ in COLUMNS], *map(cells, rep["rows"]), cells({**rep["total"], "key": "total"})]
    widths = [max(len(r[i]) for r in table) for i in range(len(COLUMNS))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table)
