"""Per-ticket cost, tokens and rounds from .agent/runs/<n>/ (state.json + cost.jsonl). No LLM.
Used by `agent-kit report` and the sandbox benchmark to compare runs."""
from pathlib import Path

from . import hooks as hook_log
from .followup import poll_text
from .llm import step_kind
from .util import read_json, read_jsonl

TOKENS = ("in", "cache_r", "out")
HOOKS = ("hook_calls", "edit_blocks", "stop_blocks", "guard_blocks", "hook_errors")
REVIEWS = ("review_skipped", "review_light", "review_full", "review_usd", "review_in", "review_cache_r")
FOLLOW = ("polls", "triaged", "triage_usd", "comment_fixes", "ci_fixes", "needs_human")  # after the PR is open
MONEY = ("usd", "review_usd", "triage_usd")  # shown with two decimals
METRICS = ("usd", "calls", "fix_rounds", "review_rounds", "escalated", *REVIEWS, *FOLLOW, *HOOKS, *TOKENS)  # summed in "total", compared by the benchmark


def _gates(state):
    rep = state.get("gates")
    return "-" if not rep else ("pass" if rep.get("ok") else "FAIL")


def hook_counts(rows, state):
    """The ticket run's hook log rows (.agent/runs/hooks.jsonl), counted."""
    mine = hook_log.rows_for(rows, state)
    def n(event, outcome):
        return sum(1 for r in mine if (event is None or r.get("event") == event) and r.get("outcome") == outcome)
    return {"hook_calls": len(mine), "edit_blocks": n("post-edit", hook_log.BLOCK),
            "stop_blocks": n("stop", hook_log.BLOCK), "guard_blocks": n("guard", hook_log.BLOCK), "hook_errors": n(None, hook_log.ERROR)}


def review_counts(state, calls):
    """Review decisions (the plan on each state.json review) by tier, and what the review calls cost."""
    tiers = [rv["plan"].get("tier") for rv in state.get("reviews") or [] if rv.get("plan")]
    rv = [c for c in calls if step_kind(c) == "review"]
    return {"review_skipped": tiers.count("skip"), "review_light": tiers.count("light"),
            "review_full": tiers.count("full"), "review_usd": round(sum(c.get("usd") or 0 for c in rv), 4),
            "review_in": sum(c.get("in") or 0 for c in rv), "review_cache_r": sum(c.get("cache_r") or 0 for c in rv)}


def follow_counts(state, calls):
    """PR follow-up: polls, triaged comments and their cost, comment and CI fix rounds, needs a human."""
    steps = [step_kind(c) for c in calls]
    return {"polls": state.get("poll_count") or len(state.get("polls") or []), "triaged": len(state.get("triage") or []),
            "triage_usd": round(sum(c.get("usd") or 0 for c in calls if step_kind(c) == "triage"), 4),
            "comment_fixes": steps.count("fix-comments"), "ci_fixes": steps.count("fix-ci"),
            "needs_human": 1 if state.get("needs_human") else 0}


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
            "gates": _gates(state), "tdd": (state.get("tdd") or {}).get("status") or "-",
            "escalated": len(state.get("escalations") or []),
        }
        row.update(review_counts(state, calls))
        row.update(follow_counts(state, calls))
        row.update(hook_counts(hook_rows, state))
        row.update({t: sum(c.get(t) or 0 for c in calls) for t in TOKENS})
        rows.append(row)
    total = {k: round(sum(r.get(k) or 0 for r in rows), 4) for k in METRICS}
    return {"rows": rows, "total": total}


COLUMNS = [("ticket", "key"), ("status", "status"), ("usd", "usd"), ("calls", "calls"), ("fix", "fix_rounds"),
           ("review", "review_rounds"), ("escal", "escalated"), ("rv_skip", "review_skipped"), ("rv_light", "review_light"),
           ("rv_full", "review_full"), ("rv_usd", "review_usd"), ("rv_in", "review_in"),
           ("rv_cache_r", "review_cache_r"), ("polls", "polls"), ("triaged", "triaged"), ("tr_usd", "triage_usd"),
           ("cm_fix", "comment_fixes"), ("ci_fix", "ci_fixes"), ("human", "needs_human"), ("gates", "gates"), ("tdd", "tdd"),
           ("hooks", "hook_calls"), ("edit_blk", "edit_blocks"), ("stop_blk", "stop_blocks"), ("guard_blk", "guard_blocks"), ("hook_err", "hook_errors"), *((t, t) for t in TOKENS)]


def run_lines(state):
    """`agent-kit log` lines for a run's red/green checks and escalated fix rounds."""
    lines = []
    if state.get("tdd_checks"):
        lines.append("## tdd")
        lines += [f"{i}. {t['status'].upper()}{' gate ' + t['gate'] if t.get('gate') else ''}, {t['secs']}s: "
                  f"{t['reason']}" + (f" ({', '.join(t['files'])})" if t.get("files") else "")
                  for i, t in enumerate(state["tdd_checks"], 1)]
    if state.get("escalations"):
        lines.append("## escalations")
        lines += [f"{e['step']} -> {e['model']}: {e['reason']}" for e in state["escalations"]]
    follow = [f"poll: {poll_text(p)}" for p in state.get("polls") or []]
    follow += [f"triage #{t['id']}: {t['action']} ({t['reason']})" for t in state.get("triage") or []]
    for f in state.get("follow_ups") or []:
        what = f"comments {', '.join(map(str, f['ids']))}" if f.get("ids") else f"sha {str(f.get('sha'))[:7]}"
        line = f"{f['step']} ({what}{', fresh session' if f.get('fresh_session') else ''}): {f['result']}"
        follow.append(line + (f" {f['commit']}" if f.get("commit") else ""))
    if state.get("needs_human"):
        follow.append(f"needs a human: {state['needs_human']['reason']}")
    follow += [f"override: {o['was']} cleared by `agent-kit address` (ci rounds {o['ci_rounds'] or 0}, "
               f"comment rounds {o['comment_rounds'] or 0})" for o in state.get("overrides") or []]
    return lines + (["## follow-up", *follow] if follow else [])


def render(rep):
    if not rep["rows"]:
        return "no runs"
    def cells(row):
        return [f"{row[k]:.2f}" if k in MONEY else str(row.get(k, "")) for _, k in COLUMNS]
    table = [[h for h, _ in COLUMNS], *map(cells, rep["rows"]), cells({**rep["total"], "key": "total"})]
    widths = [max(len(r[i]) for r in table) for i in range(len(COLUMNS))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table)
