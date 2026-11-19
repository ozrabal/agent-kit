"""The ticket loop. Plain code decides everything; the LLM is called only to
implement, fix and review.

per ticket:  pick (script) -> label in-progress -> worktree + setup -> context pack (script)
             -> implement (LLM) -> autofix + gates (script) -> fix rounds (LLM, resumed session)
             -> commit -> review (lean LLM, diff only) -> fix rounds -> ticket note + lessons
             -> kb update (script, LLM only for new files) -> push -> PR
between:     merge policy "pause": stop (or --wait: poll gh, no LLM) until the PR merges,
             then reconcile: label done, close issue, remove worktree.
"""
import json
import shutil
import time
from pathlib import Path
from string import Template

from . import gates, kb, llm
from .config import READ_ONLY_BASH, fmt, runs_dir
from .tracker import make
from .util import KIT_DIR, clip, log, out, read_json, sh, slug, write_json

IMPL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "pr_sections": {"type": "array", "items": {
            "type": "object",
            "properties": {"heading": {"type": "string"}, "body": {"type": "string"}},
            "required": ["heading", "body"]}},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "pr_sections", "lessons", "concerns"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {"type": "array", "items": {
            "type": "object",
            "properties": {"criterion": {"type": "string"}, "met": {"type": "boolean"},
                           "evidence": {"type": "string"}},
            "required": ["criterion", "met", "evidence"]}},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                           "file": {"type": "string"}, "line": {"type": "integer"},
                           "issue": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["severity", "file", "issue", "fix"]}},
        "lessons": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["gotcha", "convention"]},
                           "text": {"type": "string"}},
            "required": ["kind", "text"]}},
    },
    "required": ["criteria", "findings", "lessons"],
}
REVIEW_SYSTEM = ("You are a strict, fair senior code reviewer. You report only findings you have "
                 "verified against the code, each with a concrete failure scenario.")


def prompt(name, **vals):
    return Template((KIT_DIR / "prompts" / f"{name}.md").read_text()).safe_substitute(**vals)


class Run:
    """State of one ticket, persisted in <repo>/.agent/runs/<number>/state.json."""

    def __init__(self, cfg, number):
        self.dir = runs_dir(cfg) / str(number)
        self.file = self.dir / "state.json"
        self.s = read_json(self.file, {"number": number})
        self.cost_log = self.dir / "cost.jsonl"

    def __getitem__(self, k):
        return self.s.get(k)

    def set(self, **kv):
        self.s.update(kv)
        write_json(self.file, self.s)


def all_runs(cfg):
    d = runs_dir(cfg)
    if not d.exists():
        return []
    return [Run(cfg, int(p.name)) for p in sorted(d.iterdir(), key=lambda p: p.name.zfill(9))
            if p.is_dir() and p.name.isdigit()]


def _pathspec_excludes(patterns):
    res = []
    for p in patterns:
        g = p if "/" in p else "**/" + p
        if g.endswith("/*"):
            g = g[:-1] + "**"
        res.append(f":(exclude,glob){g}")
    return res


class Loop:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg["_root"])
        self.tr = make(cfg)
        self.base = cfg["branch"]["base"]
        self.remote = cfg["branch"]["remote"]
        self.upstream = f"{self.remote}/{self.base}"

    # --- reconcile and pick ----------------------------------------------
    def reconcile(self):
        for r in all_runs(self.cfg):
            if r["status"] not in ("pr_open", "needs_human"):
                continue
            pr = self.tr.pr_state(r["pr"]) if r["pr"] else self.tr.find_pr(r["branch"])
            if not pr:
                continue
            if pr["state"] == "MERGED":
                log(f"{r['key']}: PR merged -> done")
                self.tr.set_status(r["number"], "wip", "done")
                self.tr.close(r["number"])
                self._cleanup(r)
                r.set(status="done")
            elif pr["state"] == "CLOSED":
                log(f"{r['key']}: PR closed without merge; left as in-progress for you to decide")
                r.set(status="abandoned")

    def _cleanup(self, r):
        ws = r["ws"]
        if ws and self.cfg["workspace"]["mode"] == "worktree" and Path(ws).exists():
            sh(["git", "worktree", "remove", "--force", ws], cwd=self.root, check=False)
        sh(["git", "branch", "-D", r["branch"]], cwd=self.root, check=False)

    def waiting(self):
        return [r for r in all_runs(self.cfg) if r["status"] in ("pr_open", "needs_human")]

    def status_lines(self):
        lines = []
        for c in self.tr.candidates():
            state = "READY  " if not c["unmet"] else "BLOCKED"
            why = f" waits on {', '.join('#%d' % u for u in c['unmet'])}" if c["unmet"] else ""
            lines.append(f"{state} #{c['number']} {c['title']}{why}")
        for r in all_runs(self.cfg):
            if r["status"] not in ("done",):
                usd = sum((llm.cost_summary(r.cost_log) or {}).get(k, {}).get("usd", 0)
                          for k in (llm.cost_summary(r.cost_log) or {}))
                lines.append(f"RUN    #{r['number']} {r['key']} status={r['status']} "
                             f"pr={r['pr'] or '-'} cost=${usd:.2f}")
        return lines

    def pick(self, number=None):
        if number:
            i = self.tr.get(number)
            return {**i, "key": self.tr.key(i), "blocked_by": self.tr.blockers(i)}
        for c in self.tr.candidates():
            if not c["unmet"]:
                return c
        return None

    # --- workspace ---------------------------------------------------------
    def workspace(self, run, issue):
        w = self.cfg["workspace"]
        branch = run["branch"] or fmt(self.cfg["branch"]["name"], self.cfg,
                                      key=issue["key"].lower().lstrip("#"), slug=slug(issue["title"].split(":", 1)[-1]))
        sh(["git", "fetch", self.remote, self.base], cwd=self.root)
        if w["mode"] == "worktree":
            ws = Path(fmt(w["dir"], self.cfg)).resolve() / branch
            if not ws.exists():
                ws.parent.mkdir(parents=True, exist_ok=True)
                exists = sh(["git", "rev-parse", "--verify", branch], cwd=self.root, check=False).returncode == 0
                args = [str(ws), branch] if exists else ["-b", branch, str(ws), self.upstream]
                sh(["git", "worktree", "add", *args], cwd=self.root)
                for cmd in w["setup"]:
                    log(f"setup: {cmd}")
                    sh(fmt(cmd, self.cfg), cwd=ws, env=gates.env(self.cfg), timeout=1800)
        else:
            ws = self.root
            if out(["git", "status", "--porcelain"], cwd=ws):
                raise RuntimeError("working tree not clean; commit or stash first (or use workspace.mode=worktree)")
            exists = sh(["git", "rev-parse", "--verify", branch], cwd=ws, check=False).returncode == 0
            sh(["git", "switch", branch] if exists else ["git", "switch", "-c", branch, self.upstream], cwd=ws)
        # Seed the branch's knowledge base from the main checkout so updates stay incremental.
        src, dst = kb.kdir(self.cfg, self.root), kb.kdir(self.cfg, ws)
        if src != dst and (src / "files.jsonl").exists() and not (dst / "files.jsonl").exists():
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("index.sqlite"))
        run.set(branch=branch, ws=str(ws))
        return ws

    # --- helpers -----------------------------------------------------------
    def _has_changes(self, ws):
        return bool(out(["git", "status", "--porcelain"], cwd=ws)) or bool(
            out(["git", "rev-list", f"{self.upstream}..HEAD"], cwd=ws))

    def _commit(self, ws, msg):
        if out(["git", "status", "--porcelain"], cwd=ws):
            sh(["git", "add", "-A"], cwd=ws)
            sh(["git", "commit", "-q", "-m", msg], cwd=ws)

    def _gates(self, ws):
        gates.autofix(self.cfg, ws)
        rep = gates.run(self.cfg, ws)
        log("gates: " + ", ".join(f"{r['name']}={'ok' if r['ok'] else 'FAIL'}" for r in rep["results"]))
        return rep

    def _allow(self):
        rules = list(READ_ONLY_BASH) + list(self.cfg["implement"]["allow_bash"])
        for g in self.cfg["gates"] + self.cfg["autofix"]:
            cmd = g["cmd"] if isinstance(g, dict) else g
            rules.append(" ".join(cmd.split()[:2]) + " *")
        return [f"Bash({r})" for r in dict.fromkeys(rules)]

    def _impl(self, run, ws, text, step, resume=None):
        c = self.cfg
        res, sid = llm.claude(
            text, cwd=ws, step=step, model=c["models"]["implement"], effort=c["effort"]["implement"],
            schema=IMPL_SCHEMA, tools=c["implement"]["tools"], permission_mode=c["implement"]["permission_mode"],
            allow=self._allow(), budget=c["budget_usd"]["fix" if resume else "implement"], resume=resume,
            persist=True, timeout=c["loop"]["timeout_seconds"], cost_log=run.cost_log,
            stream_log=run.dir / f"{step.replace(':', '-')}.log")
        run.set(session=sid or resume, impl=res)
        return res

    def _review(self, run, ws, issue):
        c = self.cfg
        ex = _pathspec_excludes(c["review"]["exclude"])
        rng = f"{self.upstream}...HEAD"
        stat = out(["git", "diff", "--stat", rng, "--", ".", *ex], cwd=ws)
        diff = out(["git", "diff", "-U3", rng, "--", ".", *ex], cwd=ws)
        conv = kb.kdir(c, ws) / "conventions.md"
        text = prompt("review", key=issue["key"], title=issue["title"], body=issue["body"] or "",
                      conventions=conv.read_text() if conv.exists() else "(none recorded)",
                      gate_names=", ".join(g["name"] for g in map(gates._norm, c["gates"])),
                      base=self.upstream, stat=stat,
                      diff=clip(diff, c["review"]["diff_chars"]) + (
                          "\n(diff truncated: Read the remaining files listed in the stat)" if len(diff) > c["review"]["diff_chars"] else ""))
        res, _ = llm.claude(text, cwd=ws, step="review", model=c["models"]["review"], effort=c["effort"]["review"],
                            schema=REVIEW_SCHEMA, lean=True, system=REVIEW_SYSTEM, tools=c["review"]["tools"],
                            budget=c["budget_usd"]["review"], timeout=c["loop"]["timeout_seconds"],
                            cost_log=run.cost_log, stream_log=run.dir / f"review-{len(run['reviews'] or []) + 1}.log")
        block = [f for f in res["findings"] if f["severity"] in c["review"]["block_on"]]
        unmet = [x for x in res["criteria"] if not x["met"]]
        res["pass"] = not block and not unmet
        log(f"review: {'PASS' if res['pass'] else 'FAIL'} ({len(block)} blocking, {len(unmet)} unmet criteria, "
            f"{len(res['findings']) - len(block)} minor)")
        hist = (run["reviews"] or []) + [res]
        run.set(reviews=hist)
        return res, block, unmet

    # --- one ticket --------------------------------------------------------
    def do_ticket(self, issue, dry=False):
        c, n, key = self.cfg, issue["number"], issue["key"]
        run = Run(c, n)
        ctx_cwd = self.root
        related = [self.tr.key(self.tr.get(b)) for b in issue.get("blocked_by", [])]
        ctx = kb.context(c, ctx_cwd, f"{issue['title']}\n{issue['body']}", key=key, related_keys=related) \
            if (kb.kdir(c, ctx_cwd) / "files.jsonl").exists() else "(no knowledge base yet)"
        gate_names = ", ".join(g["name"] for g in map(gates._norm, c["gates"])) or "(none)"
        impl_prompt = prompt("implement", key=key, number=n, title=issue["title"], body=issue["body"] or "",
                             context=ctx, gate_names=gate_names, pr_sections=", ".join(c["pr"]["sections"]))
        if dry:
            print(impl_prompt)
            log(f"dry run: {key} would start; prompt is {len(impl_prompt)} chars (~{len(impl_prompt) // 4} tokens)")
            return True

        log(f"=== {key} (#{n}): {issue['title']}")
        run.set(key=key, title=issue["title"], status="implementing")
        (run.dir / "context.md").write_text(ctx)
        if c["tracker"]["labels"]["wip"] not in self.tr.get(n)["labels"]:
            self.tr.set_status(n, "ready", "wip")
        failure = None
        try:
            ws = self.workspace(run, issue)
            if run["session"] and self._has_changes(ws):
                log("resuming: existing changes and session found, skipping implement")
                impl = run["impl"]
            else:
                impl = self._impl(run, ws, impl_prompt, "implement")
            if not self._has_changes(ws):
                raise RuntimeError("implementer produced no changes")
            failure = self._quality_rounds(run, ws, issue)
            impl = run["impl"]
        except (RuntimeError, llm.LLMError) as e:
            failure = {"reason": str(e)[:3000]}
            ws = Path(run["ws"]) if run["ws"] else None
            impl = run["impl"] or {"summary": "", "pr_sections": [], "lessons": [], "concerns": []}
        if ws is None or not self._has_changes(ws):
            run.set(status="failed", failure=failure)
            log(f"{key}: failed before any change: {failure['reason'][:300] if failure else ''}")
            return False
        return self._ship(run, ws, issue, impl, failure)

    def _quality_rounds(self, run, ws, issue):
        c, key = self.cfg, issue["key"]
        review_round = 0
        run.set(fix_rounds=run["fix_rounds"] or 0)
        while True:
            rep = self._gates(ws)
            fix_round = 0
            while not rep["ok"] and fix_round < c["loop"]["max_fix_rounds"]:
                fix_round += 1
                run.set(fix_rounds=run["fix_rounds"] + 1)
                details = gates.render({"results": [r for r in rep["results"] if not r["ok"]]})
                self._impl(run, ws, prompt("fix", key=key, reason="These gates fail:", details=details),
                           f"fix-gates:{fix_round}", resume=run["session"])
                rep = self._gates(ws)
            run.set(gates=rep)
            if not rep["ok"]:
                return {"reason": "gates still failing after fix rounds", "details": gates.render(rep)}
            self._commit(ws, f"{key}: {issue['title'].split(':', 1)[-1].strip()}" if review_round == 0
                         else f"{key}: address review round {review_round}")
            rv, block, unmet = self._review(run, ws, issue)
            if rv["pass"]:
                return None
            if review_round >= c["loop"]["max_review_rounds"]:
                return {"reason": "review still failing after fix rounds", "details": _findings_md(block, unmet)}
            review_round += 1
            run.set(status=f"fixing-review-{review_round}")
            self._impl(run, ws, prompt("fix", key=key, reason="Code review found problems:",
                                       details=_findings_md(block, unmet)),
                       f"fix-review:{review_round}", resume=run["session"])

    def _ship(self, run, ws, issue, impl, failure):
        c, n, key = self.cfg, issue["number"], issue["key"]
        self._commit(ws, f"{key}: work in progress")
        lessons = list(impl.get("lessons") or [])
        reviews = run["reviews"] or []
        if not failure:
            numstat = out(["git", "diff", "--numstat", f"{self.upstream}...HEAD", "--", ".",
                           *_pathspec_excludes(c["review"]["exclude"])], cwd=ws)
            files = "\n".join(f"- `{p}` (+{a} -{d})" for a, d, p in
                              (l.split("\t") for l in numstat.splitlines() if l.count("\t") == 2))
            kb.write_ticket(c, ws, key, issue["title"].split(":", 1)[-1].strip(), n, impl["summary"], files, lessons)
            items = [("gotchas", l, key) for l in lessons]
            for rv in reviews:
                items += [(x["kind"], x["text"], key) for x in rv.get("lessons", [])]
            added = kb.add_lessons(c, ws, items)
            kb.update(c, ws, cost_log=run.cost_log)
            self._commit(ws, f"{key}: update knowledge base")
            log(f"kb: ticket note written, {added} lessons added")
        sh(["git", "push", "-u", self.remote, run["branch"]], cwd=ws)
        body = self._pr_body(issue, impl, run, failure)
        bf = run.dir / "pr.md"
        bf.write_text(body)
        pr = self.tr.find_pr(run["branch"])
        if pr and pr["state"] == "OPEN":
            self.tr.edit_pr_body(pr["number"], bf)
            url = pr["url"]
        else:
            url = self.tr.create_pr(self.base, run["branch"], issue["title"], bf,
                                    draft=bool(failure) and c["pr"]["draft_on_fail"])
        run.set(pr=url, status="needs_human" if failure else "pr_open", failure=failure)
        cost = llm.cost_summary(run.cost_log) or {}
        log(f"{key}: {'DRAFT PR (needs you)' if failure else 'PR ready for review'}: {url}")
        log("cost: " + ", ".join(f"{k} ${v['usd']:.2f}/{v['calls']}x" for k, v in cost.items())
            + f" | total ${sum(v['usd'] for v in cost.values()):.2f}")
        return not failure

    def _pr_body(self, issue, impl, run, failure):
        c = self.cfg
        parts = [f"Closes #{issue['number']}", ""]
        secs = {s["heading"].strip().lstrip("#").strip().lower(): s["body"] for s in impl.get("pr_sections", [])}
        for h in c["pr"]["sections"]:
            parts += [f"## {h}", "", secs.get(h.lower(), impl.get("summary", "") if h.lower() == "summary" else "-"), ""]
        rep = run["gates"]
        if rep:
            parts += ["## Gates", "", "\n".join(f"- {'pass' if r['ok'] else 'FAIL'}: {r['name']}" for r in rep["results"]), ""]
        reviews = run["reviews"] or []
        if c["pr"]["include_review"] and reviews:
            last = reviews[-1]
            minor = [f for f in last["findings"] if f["severity"] not in c["review"]["block_on"]]
            parts += ["## Automated review", "",
                      f"{'Passed' if last.get('pass') else 'Not passed'} after {len(reviews)} round(s)."]
            parts += [f"- [x] {x['criterion']}" if x["met"] else f"- [ ] {x['criterion']}" for x in last["criteria"]]
            if minor:
                parts += ["", "Minor findings left for you:"] + [f"- `{f['file']}`: {f['issue']}" for f in minor]
            parts.append("")
        if impl.get("concerns"):
            parts += ["## Concerns", "", *[f"- {x}" for x in impl["concerns"]], ""]
        if failure:
            parts += ["## Needs a human", "", failure["reason"], "", failure.get("details", ""), ""]
        return "\n".join(parts).rstrip() + "\n"

    # --- the loop ----------------------------------------------------------
    def run(self, ticket=None, wait=False, max_tickets=None, dry=False):
        done = 0
        while True:
            self.reconcile()
            self.tr.issues(refresh=True)
            pending = self.waiting()
            if not ticket and len(pending) >= self.cfg["merge"]["max_open_prs"] and not dry:
                names = ", ".join(f"{r['key']} {r['pr']}" for r in pending)
                if not wait:
                    log(f"paused: waiting for you to merge {names}. Run again after merging (or use --wait).")
                    return 0
                log(f"waiting for merge: {names} (polling every {self.cfg['merge']['poll_seconds']}s, no LLM)")
                time.sleep(self.cfg["merge"]["poll_seconds"])
                continue
            if max_tickets is not None and done >= max_tickets:
                return 0
            issue = self.pick(ticket)
            if not issue:
                log("nothing unblocked to do.")
                for l in self.status_lines():
                    log("  " + l)
                return 0
            ok = self.do_ticket(issue, dry=dry)
            done += 1
            if dry or ticket or not ok:
                return 0 if ok else 2


def _findings_md(block, unmet):
    lines = [f"- unmet criterion: {x['criterion']} ({x['evidence']})" for x in unmet]
    lines += [f"- [{f['severity']}] `{f['file']}`{':' + str(f['line']) if f.get('line') else ''}: "
              f"{f['issue']} -> fix: {f['fix']}" for f in block]
    return "\n".join(lines)
