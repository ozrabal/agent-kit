"""Per-ticket cost, tokens and rounds from .agent/runs/<n>/ (state.json + cost.jsonl). No LLM.
Used by `agent-kit report` and the sandbox benchmark to compare runs."""
import time
from pathlib import Path

from . import hooks as hook_log
from .followup import poll_text
from .llm import step_kind
from .config import RETRO_LOG
from .util import plural, read_json, read_jsonl

TOKENS = ("in", "cache_r", "out")
HOOKS = ("hook_calls", "edit_blocks", "stop_blocks", "guard_blocks", "gates_blocks", "hook_errors")  # gates: in-session `agent-kit gates`
REVIEWS = ("review_skipped", "review_light", "review_full", "review_usd", "review_in", "review_cache_r")
FOLLOW = ("polls", "triaged", "triage_usd", "comment_fixes", "ci_fixes", "needs_human")  # after the PR is open
REFINE = ("refine_checks", "refine_failed", "refine_proposed", "refine_usd", "refine_applied", "split_created")
PARALLEL = ("parallel", "lock_stale", "lock_busy")  # ran in a batch; stale locks removed; skipped as locked
MONEY = ("usd", "review_usd", "triage_usd", "refine_usd")  # shown with two decimals
METRICS = ("usd", "calls", "fix_rounds", "review_rounds", "escalated", *REVIEWS, *FOLLOW, *REFINE, *PARALLEL,
           *HOOKS, *TOKENS)  # summed in "total", compared by the benchmark


def _gates(state):
    rep = state.get("gates")
    return "-" if not rep else ("pass" if rep.get("ok") else "FAIL")


def hook_counts(rows, state):
    """The ticket run's hook log rows (.agent/runs/hooks.jsonl), counted."""
    mine = hook_log.rows_for(rows, state)
    def n(event, outcome):
        return sum(1 for r in mine if (event is None or r.get("event") == event) and r.get("outcome") == outcome)
    return {"hook_calls": len(mine), "edit_blocks": n("post-edit", hook_log.BLOCK),
            "stop_blocks": n("stop", hook_log.BLOCK), "guard_blocks": n("guard", hook_log.BLOCK),
            "gates_blocks": n("gates", hook_log.BLOCK), "hook_errors": n(None, hook_log.ERROR)}


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


def refine_counts(state, calls):
    """The refine step: checks made, the last one failing, a proposal posted, its cost, applied, split issues made."""
    rf, ap = state.get("refine") or {}, state.get("refine_apply") or {}
    return {"refine_checks": state.get("refine_checks") or 0, "refine_failed": 1 if rf and not rf.get("ok") else 0,
            "refine_proposed": 1 if rf.get("proposed") else 0,
            "refine_usd": round(sum(c.get("usd") or 0 for c in calls if step_kind(c) == "refine"), 4),
            "refine_applied": 1 if ap.get("done") else 0, "split_created": len(ap.get("created") or [])}


def parallel_counts(state):
    events = [x.get("event") for x in state.get("locks") or []]
    batch = (state.get("parallel") or {}).get("batch") or []
    return {"parallel": 1 if len(batch) > 1 else 0, "lock_stale": events.count("stale"),
            "lock_busy": events.count("busy")}


def newer(rows, since):
    """Rows (cost, hook log) at or after since (unix ts); all of them when since is None."""
    return [r for r in rows if since is None or (r.get("ts") or 0) >= since]


def collect(runs_dir, since=None):
    """{"rows": [one per ticket run], "total": sums}. since: unix ts; older calls are ignored
    and runs with no newer calls are left out."""
    rows = []
    runs_dir = Path(runs_dir)
    hook_rows = newer(read_jsonl(runs_dir / hook_log.LOG_NAME), since)
    dirs = runs_dir.iterdir() if runs_dir.is_dir() else []
    for d in sorted((p for p in dirs if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        calls = newer(read_jsonl(d / "cost.jsonl"), since)
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
        row.update(refine_counts(state, calls))
        row.update(parallel_counts(state))
        row.update(hook_counts(hook_rows, state))
        row.update({t: sum(c.get(t) or 0 for c in calls) for t in TOKENS})
        rows.append(row)
    total = {k: round(sum(r.get(k) or 0 for r in rows), 4) for k in METRICS}
    return {"rows": rows, "total": total}


COLUMNS = [("ticket", "key"), ("status", "status"), ("usd", "usd"), ("calls", "calls"), ("fix", "fix_rounds"),
           ("review", "review_rounds"), ("escal", "escalated"), ("rv_skip", "review_skipped"), ("rv_light", "review_light"),
           ("rv_full", "review_full"), ("rv_usd", "review_usd"), ("rv_in", "review_in"),
           ("rv_cache_r", "review_cache_r"), ("polls", "polls"), ("triaged", "triaged"), ("tr_usd", "triage_usd"),
           ("cm_fix", "comment_fixes"), ("ci_fix", "ci_fixes"), ("human", "needs_human"), ("rf_chk", "refine_checks"), ("rf_fail", "refine_failed"),
           ("rf_prop", "refine_proposed"), ("rf_usd", "refine_usd"), ("rf_appl", "refine_applied"), ("rf_split", "split_created"),
           ("par", "parallel"), ("lk_stale", "lock_stale"), ("lk_busy", "lock_busy"), ("gates", "gates"), ("tdd", "tdd"),
           ("hooks", "hook_calls"), ("edit_blk", "edit_blocks"), ("stop_blk", "stop_blocks"), ("guard_blk", "guard_blocks"), ("gates_blk", "gates_blocks"), ("hook_err", "hook_errors"), *((t, t) for t in TOKENS)]


def retro_line(runs_dir):
    """`agent-kit report` footer from .agent/runs/retro.jsonl (one row per `agent-kit retro`); "" before the first."""
    rows = read_jsonl(Path(runs_dir) / RETRO_LOG)
    if not rows:
        return ""
    last = rows[-1]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last.get("ts") or 0))
    return (f"retro: {plural(len(rows), 'run')}, LLM ${sum(r.get('usd') or 0 for r in rows):.2f}, "
            f"{sum(len(r.get('rules') or []) for r in rows)} rules and {sum(r.get('gotchas') or 0 for r in rows)} "
            f"gotchas proposed; last {when} ({last.get('mode')}: {len(last.get('rules') or [])} rules, "
            f"{last.get('gotchas') or 0} gotchas, {last.get('config') or 0} config)")


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
    return lines + (["## follow-up", *follow] if follow else []) + refine_lines(state) + parallel_lines(state)


def refine_note(state):
    """One line on a run's refine result for `agent-kit status`; "" when there is nothing to say."""
    rf, ap = state.get("refine") or {}, state.get("refine_apply") or {}
    if ap.get("done"):
        return "applied: " + (f"split -> {', '.join('#%d' % x for x in ap['created'])}" if ap.get("created")
                              else "criteria written into the body")
    if rf.get("proposed"):
        return f"proposal posted ({rf.get('source')})"
    if rf and not rf.get("ok"):
        return "not ready: " + "; ".join(rf.get("reasons") or [])
    return ""


def refine_lines(state):
    rf, ap = state.get("refine"), state.get("refine_apply") or {}
    if not rf and not ap:
        return []
    lines = ["## refine"]
    if rf:
        verdict = "READY" if rf.get("ok") else f"NOT READY ({'; '.join(rf.get('reasons') or [])})"
        lines.append(f"{rf.get('mode')}: {verdict}, {state.get('refine_checks') or 1} check(s)")
        if rf.get("proposed"):
            lines.append(f"proposal posted ({rf.get('source')}): {rf.get('criteria_proposed', 0)} criteria, "
                         f"{rf.get('split', 0)} split, {rf.get('questions', 0)} question(s)")
    if ap:
        lines.append(refine_note({"refine_apply": {**ap, "done": True}}) + ("" if ap.get("done") else " (unfinished)"))
    return lines


def parallel_lines(state):
    p, lines = state.get("parallel"), []
    if p:
        left = "; ".join(f"#{x['number']} {x['why']}" for x in p.get("left") or [])
        lines += ["## parallel", f"batch {', '.join('#%d' % x for x in p['batch'])} in {p.get('slots')} slot(s)"
                  + (f"; left out: {left}" if left else "")]
        if p.get("pid"):
            lines.append(f"child pid {p['pid']}: exit {p.get('exit')} after {p.get('secs')}s")
    if state.get("locks"):
        lines += ["## locks", *[f"{x['event']} pid {x.get('pid')}" for x in state["locks"]]]
    return lines


def render(rep):
    if not rep["rows"]:
        return "no runs"
    def cells(row):
        return [f"{row[k]:.2f}" if k in MONEY else str(row.get(k, "")) for _, k in COLUMNS]
    table = [[h for h, _ in COLUMNS], *map(cells, rep["rows"]), cells({**rep["total"], "key": "total"})]
    widths = [max(len(r[i]) for r in table) for i in range(len(COLUMNS))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table)
