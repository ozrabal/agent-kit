"""agent-kit command line."""
import argparse
import json
import sys
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

    a = ap.parse_args(argv)
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

    if a.cmd == "gates":
        if a.fix:
            gates.autofix(cfg, here)
        rep = gates.run(cfg, here, only=a.names or None)
        print(gates.render(rep))
        return 0 if rep["ok"] else 1

    if a.cmd == "kb":
        if a.kbcmd == "update":
            kb.update(cfg, here, full=a.full, use_llm=not a.no_llm,
                      cost_log=Path(cfg["_root"]) / ".agent" / "runs" / "kb-cost.jsonl")
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
        rep = report.collect(Path(cfg["_root"]) / ".agent" / "runs", since=a.since)
        print(json.dumps(rep, indent=2) if a.json else report.render(rep))
        return 0

    if a.cmd == "log":
        d = Path(cfg["_root"]) / ".agent" / "runs" / str(a.ticket)
        logs = sorted(d.glob("*.log"), key=lambda f: f.stat().st_mtime)
        if a.step:
            logs = [f for f in logs if f.stem == a.step]
        if not logs:
            die(f"no Claude logs in {d}" + (f" for step {a.step}" if a.step else ""))
        ws = (json.loads((d / "state.json").read_text()).get("ws") if (d / "state.json").exists() else None)
        for f in logs:
            print(f"## {f.stem}")
            print("\n".join(llm.render_log(f, cwd=ws)) or "(empty)")
        return 0

    if not cfg["_configured"]:
        die("no .agent/config.json in this repo: run `agent-kit init` first")
    from .loop import Loop
    lp = Loop(cfg)
    if a.cmd == "status":
        lp.reconcile()
        print("\n".join(lp.status_lines()) or "no tickets")
    elif a.cmd == "next":
        i = lp.pick()
        print(json.dumps({k: i[k] for k in ("number", "key", "title", "blocked_by")} if i else None))
    elif a.cmd == "reconcile":
        lp.reconcile()
    elif a.cmd == "run":
        return lp.run(ticket=a.ticket, wait=a.wait, max_tickets=a.max, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
