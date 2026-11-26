"""agent-kit command line."""
import argparse
import json
import sys
import time
from pathlib import Path

from . import config, gates, kb, llm
from .util import die, main_root, repo_root


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-kit", description="Ticket loop and project knowledge base for coding agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="detect stack, write .agent/config.json, seed .knowledge/")
    p.add_argument("--force", action="store_true")
    p.add_argument("--claude-md", action="store_true", help="add a short KB section to CLAUDE.md")

    p = sub.add_parser("run", help="run the ticket loop")
    p.add_argument("--ticket", type=int, help="run this issue number only")
    p.add_argument("--wait", action="store_true", help="poll for PR merges instead of stopping (no LLM cost)")
    p.add_argument("--max", type=int, help="stop after N tickets")
    p.add_argument("--dry-run", action="store_true", help="print the implement prompt for the next ticket; change nothing")

    p = sub.add_parser("address", help="handle new PR review comments and failed CI for a ticket's open PR now")
    p.add_argument("ticket", type=int)

    p = sub.add_parser("refine", help="check a ticket is ready (criteria, size); if not, post an LLM proposal and label it")
    p.add_argument("ticket", type=int)
    p.add_argument("--apply", action="store_true", help="apply the posted proposal: criteria into the body, or create the split issues")

    sub.add_parser("status", help="ready/blocked tickets and runs with cost")
    sub.add_parser("next", help="print the next unblocked ticket as JSON")
    sub.add_parser("reconcile", help="mark merged PRs done and clean up worktrees")

    p = sub.add_parser("log", help="show what Claude did in a ticket run (tool calls, errors, results)")
    p.add_argument("ticket", type=int)
    p.add_argument("step", nargs="?", help="only this step, e.g. implement, review-1, fix-gates-1")

    p = sub.add_parser("doctor", help="check tools, auth, config, labels, base branch, gates and knowledge base")
    p.add_argument("--no-gates", action="store_true", help="skip running the gates (they can be slow)")

    p = sub.add_parser("report", help="cost, tokens and rounds per ticket run")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--since", type=int, help="only LLM calls at or after this unix timestamp")

    p = sub.add_parser("retro", help="find repeated gate errors, review findings and denials across runs; propose rules and gotchas")
    p.add_argument("--since", type=int, help="only runs with LLM calls at or after this unix timestamp")
    p.add_argument("--no-llm", action="store_true", help="print the aggregate only; no LLM call, nothing written")

    p = sub.add_parser("hooks", help="install Claude Code hooks: scope guard, context at session start, file gates after edits, gates before stopping")
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--user", action="store_true", help="~/.claude/settings.json instead of the repo's")

    p = sub.add_parser("hook")  # called by Claude Code with the hook JSON on stdin; not for people
    p.add_argument("event")

    p = sub.add_parser("gates", help="run quality gates; prints only failures")
    p.add_argument("names", nargs="*")
    p.add_argument("--fix", action="store_true", help="run autofix commands first")

    p = sub.add_parser("kb", help="knowledge base")
    kbs = p.add_subparsers(dest="kbcmd", required=True)
    q = kbs.add_parser("update", help="incremental update (only changed files are re-parsed)")
    q.add_argument("--full", action="store_true")
    q.add_argument("--no-llm", action="store_true", help="skip LLM summaries")
    q = kbs.add_parser("context", help="print a context pack for a task")
    q.add_argument("query", nargs="+", help="task text, or #<issue number>")
    kbs.add_parser("stats")

    sub.metavar = "{" + ",".join(c for c in sub.choices if c != "hook") + "}"  # `hook` is for Claude Code only
    a = ap.parse_args(argv)
    if a.cmd == "hook":
        from . import hooks
        code, msg, stdout = hooks.main(a.event, sys.stdin.read())
        if stdout:
            print(stdout)
        if msg:
            print(msg, file=sys.stderr)
        return code
    if a.cmd == "init":
        from .init import init
        return init(force=a.force, claude_md=a.claude_md)

    cfg = config.load()
    here = repo_root()  # current checkout (may be a worktree)

    if a.cmd == "doctor":
        from . import doctor
        res = doctor.check(cfg, run_gates=None if a.no_gates else (lambda: gates.run(cfg, here)))
        print(doctor.render(res))
        return 0 if doctor.all_ok(res) else 1

    if a.cmd == "hooks":
        from . import hooks
        path = hooks.settings_path(None if a.user else main_root())
        if a.action == "install":
            hooks.install(path)
        else:
            hooks.uninstall(path)
        return 0

    if a.cmd == "gates":
        t0 = time.time()
        if a.fix:
            gates.autofix(cfg, here)
        rep = gates.run(cfg, here, only=a.names or None)
        print(gates.render(rep), flush=True)
        from . import hooks
        note = hooks.record_gates(cfg, here, rep, t0)  # only inside a loop run's worktree
        if note:
            print(note, file=sys.stderr)
        return 0 if rep["ok"] else 1

    if a.cmd == "kb":
        if a.kbcmd == "update":
            kb.update(cfg, here, full=a.full, use_llm=not a.no_llm,
                      cost_log=config.runs_dir(cfg) / "kb-cost.jsonl")
        elif a.kbcmd == "stats":
            print(json.dumps(kb.stats(cfg, here), indent=2))
        else:
            text, key, related = " ".join(a.query), None, []
            if text.startswith("#") and text[1:].isdigit():
                from .tracker import make
                tr = make(cfg)
                i = tr.get(int(text[1:]))
                text, key = f"{i['title']}\n{i['body']}", tr.key(i)
                related = [tr.key(tr.get(b)) for b in tr.blockers(i)]
            print(kb.context(cfg, here, text, key=key, related_keys=related))
        return 0

    if a.cmd == "report":
        from . import report
        rep = report.collect(config.runs_dir(cfg), since=a.since)
        footer = report.retro_line(config.runs_dir(cfg))
        print(json.dumps(rep, indent=2) if a.json else report.render(rep) + (f"\n\n{footer}" if footer else ""))
        return 0

    if a.cmd == "retro":
        from . import retro
        code, text = retro.run(cfg, here, since=a.since, use_llm=not a.no_llm)
        print(text)
        return code

    if a.cmd == "log":
        from . import hooks
        from .util import read_json
        d = config.runs_dir(cfg) / str(a.ticket)
        logs = sorted(d.glob("*.log"), key=lambda f: f.stat().st_mtime)
        if a.step:
            logs = [f for f in logs if f.stem == a.step]
        state = read_json(d / "state.json", {}) or {}
        ws = state.get("ws")
        hook_rows = hooks.rows_for(hooks.read_rows(cfg), state, step=a.step)
        plans = [rv["plan"] for rv in state.get("reviews") or [] if rv.get("plan")]
        from .report import run_lines
        notes = [] if a.step else run_lines(state)
        if not logs and not hook_rows and not plans and not notes:
            die(f"no Claude logs in {d}" + (f" for step {a.step}" if a.step else ""))
        for f in logs:
            print(f"## {f.stem}")
            print("\n".join(llm.render_log(f, cwd=ws)) or "(empty)")
        if plans and not a.step:
            print("## review plan")
            for i, p in enumerate(plans, 1):
                model = f" {p['model']}" if p["run"] else ""
                print(f"{i}. {p['tier'].upper()}{model}: {p['reason']}")
        if notes:
            print("\n".join(notes))
        if hook_rows:
            print("## hooks")
            print("\n".join(hooks.render_row(r, cwd=ws) for r in hook_rows))
        return 0

    if not cfg["_configured"]:
        die("no .agent/config.json in this repo: run `agent-kit init` first")
    from .loop import Loop
    lp = Loop(cfg)
    if a.cmd == "status":
        lp.reconcile()
        print("\n".join(lp.status_lines()) or "no tickets")
    elif a.cmd == "next":
        i = lp.pick(dry=True)
        print(json.dumps({k: i[k] for k in ("number", "key", "title", "blocked_by")} if i else None))
    elif a.cmd == "reconcile":
        lp.reconcile()
    elif a.cmd == "refine":
        if a.apply:
            return lp.refine_apply(a.ticket)
        i = lp.tr.get(a.ticket)
        return 0 if lp.refine({**i, "key": lp.tr.key(i)}, mode="auto") else 1
    elif a.cmd == "address":
        lp.reconcile()
        return lp.address_now(a.ticket)
    elif a.cmd == "run":
        return lp.run(ticket=a.ticket, wait=a.wait, max_tickets=a.max, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
