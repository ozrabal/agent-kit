"""Per-ticket cost, tokens and rounds from .agent/runs/<n>/ (state.json + cost.jsonl). No LLM.
Used by `agent-kit report` and the sandbox benchmark to compare runs."""
from pathlib import Path

from . import hooks as hook_log
from .llm import step_kind
from .util import read_json, read_jsonl

TOKENS = ("in", "cache_r", "out")
HOOKS = ("hook_calls", "edit_blocks", "stop_blocks", "hook_errors")
METRICS = ("usd", "calls", "fix_rounds", "review_rounds", *HOOKS, *TOKENS)  # summed in "total", compared by the benchmark


def _gates(state):
    rep = state.get("gates")
    return "-" if not rep else ("pass" if rep.get("ok") else "FAIL")


def hook_counts(rows, state):
    """The ticket run's hook log rows (.agent/runs/hooks.jsonl), counted."""
    mine = hook_log.rows_for(rows, state)
    def n(event, outcome):
        return sum(1 for r in mine if (event is None or r.get("event") == event) and r.get("outcome") == outcome)
    return {"hook_calls": len(mine), "edit_blocks": n("post-edit", hook_log.BLOCK),
            "stop_blocks": n("stop", hook_log.BLOCK), "hook_errors": n(None, hook_log.ERROR)}


def collect(runs_dir, since=None):
    """{"rows": [one per ticket run], "total": sums}. since: unix ts; older calls are ignored
    and runs with no newer calls are left out."""
    rows = []
    runs_dir = Path(runs_dir)
    hook_rows = [h for h in read_jsonl(runs_dir / hook_log.LOG_NAME) if since is None or (h.get("ts") or 0) >= since]
    dirs = runs_dir.iterdir() if runs_dir.is_dir() else []
    for d in sorted((p for p in dirs if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        calls = [c for c in read_jsonl(d / "cost.jsonl") if since is None or (c.get("ts") or 0) >= since]
        if since is not None and not calls:
            continue
        state = read_json(d / "state.json", {}) or {}
        steps = [step_kind(c) for c in calls]
        row = {
            "ticket": int(d.name), "key": state.get("key") or f"#{d.name}", "title": state.get("title") or "",
            "status": state.get("status") or "-", "usd": round(sum(c.get("usd") or 0 for c in calls), 4),
            "calls": len(calls), "fix_rounds": steps.count("fix-gates"), "review_rounds": steps.count("review"),
            "gates": _gates(state),
        }
        row.update(hook_counts(hook_rows, state))
        row.update({t: sum(c.get(t) or 0 for c in calls) for t in TOKENS})
        rows.append(row)
    total = {k: round(sum(r.get(k) or 0 for r in rows), 4) for k in METRICS}
    return {"rows": rows, "total": total}


COLUMNS = [("ticket", "key"), ("status", "status"), ("usd", "usd"), ("calls", "calls"), ("fix", "fix_rounds"),
           ("review", "review_rounds"), ("gates", "gates"),
           ("hooks", "hook_calls"), ("edit_blk", "edit_blocks"), ("stop_blk", "stop_blocks"), ("hook_err", "hook_errors"), *((t, t) for t in TOKENS)]


def render(rep):
    if not rep["rows"]:
        return "no runs"
    def cells(row):
        return [f"{row[k]:.2f}" if k == "usd" else str(row.get(k, "")) for _, k in COLUMNS]
    table = [[h for h, _ in COLUMNS], *map(cells, rep["rows"]), cells({**rep["total"], "key": "total"})]
    widths = [max(len(r[i]) for r in table) for i in range(len(COLUMNS))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table)
