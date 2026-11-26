"""The ticket loop. Plain code decides everything; the LLM is called only to
implement, fix and review.

per ticket:  pick (script) -> label in-progress -> worktree + setup -> context pack (script)
             -> implement (LLM) -> autofix + gates (script) -> fix rounds (LLM, resumed session)
             -> commit -> review (lean LLM, diff only) -> fix rounds -> ticket note + lessons
             -> kb update (script, LLM only for new files) -> push -> PR
between:     merge policy "pause": stop (or --wait: poll gh, no LLM) until the PR merges,
             then reconcile: label done, close issue, remove worktree.
"""
import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from string import Template

from . import followup, gates, hooks, kb, llm, parallel, refine, report, tdd
from .config import FIXING_REVIEW, IMPLEMENTING, READ_ONLY_BASH, fmt, runs_dir
from .tracker import make
from .util import KIT_DIR, clip, compact, log, out, read_json, sh, slug, write_json

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
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"comments": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "action": {"type": "string", "enum": ["fix", "reply", "ignore"]},
                       "reason": {"type": "string"}, "reply_draft": {"type": "string"}},
        "required": ["id", "action", "reason", "reply_draft"]}}},
    "required": ["comments"],
}


def prompt(name, **vals):
    return Template((KIT_DIR / "prompts" / f"{name}.md").read_text()).safe_substitute(**vals)


def review_system(cfg):
    """The review's stable part (role, gates, instructions) as its system prompt: same for every
    ticket of a project, so repeated reviews can read it from the prompt cache."""
    return prompt("review-system", gate_names=", ".join(g["name"] for g in map(gates._norm, cfg["gates"])) or "(none)")


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


def has_criteria(body, heading="acceptance criteria"):
    """True when a ticket states acceptance criteria: such a heading (refine.criteria_heading) or a markdown checklist."""
    return refine.criteria(body, heading) is not None or bool(re.search(r"(?m)^\s*[-*]\s+\[[ xX]\]", body or ""))


TIERS = ("skip", "light", "full")
FOLLOWING = ("pr_open", "fixing-review-comments", "fixing-review-ci")  # polled; a fix status = interrupted fix
POLLS_KEPT = 50  # state.json keeps the last polls only; --wait polls every merge.poll_seconds
LOCKS_KEPT = 40  # state.json keeps the last lock events (taken, released, stale, busy)


def review_plan(cfg, changed, diff_lines, body="", reviewed=None):
    """Whether the review runs and with which model: {"run", "model", "reason", "tier": skip|light|full}. Pure.
    reviewed: the highest tier an earlier round of this ticket ran; a re-review never goes below it."""
    p = _plan(cfg, changed, diff_lines, body)
    if reviewed and TIERS.index(p["tier"]) < TIERS.index(reviewed):
        model = cfg["models"]["review" if reviewed == "full" else "review_light"]
        p = {"run": True, "model": model, "tier": reviewed,
             "reason": f"{p['reason']}; kept {reviewed}: an earlier round was reviewed"}
    return p


def _plan(cfg, changed, diff_lines, body):
    r, m = cfg["review"], cfg["models"]
    if kb.docs_only(changed, r["skip_paths"]):
        return {"run": False, "model": None, "reason": "docs only", "tier": "skip"}
    risky = next((p for p in changed if r["risky_paths"] and kb.matches(p, r["risky_paths"])), None)
    if risky:
        return {"run": True, "model": m["review"], "reason": f"risky path {risky}", "tier": "full"}
    if changed and r["skip_under_lines"] > 0 and diff_lines <= r["skip_under_lines"] \
            and not has_criteria(body, cfg["refine"]["criteria_heading"]):
        return {"run": False, "model": None, "tier": "skip",
                "reason": f"{diff_lines} changed lines <= review.skip_under_lines ({r['skip_under_lines']})"}
    if not changed:
        return {"run": True, "model": m["review"], "reason": "no reviewable files listed", "tier": "full"}
    if diff_lines > r["light_max_lines"]:
        return {"run": True, "model": m["review"], "tier": "full",
                "reason": f"{diff_lines} changed lines > review.light_max_lines ({r['light_max_lines']})"}
    return {"run": True, "model": m["review_light"], "reason": f"{diff_lines} changed lines, ordinary paths",
            "tier": "light"}


def fix_model(cfg, rnd, last):
    """The model for fix round rnd of last: models.escalate on the last round when set, else models.implement."""
    m = cfg["models"]
    return m["escalate"] if m["escalate"] and rnd == last else m["implement"]


class Loop:
    def __init__(self, cfg, claude=llm.claude, tracker=None, child_cmd=None):
        self.cfg = cfg
        self.claude = claude  # tests pass a fake (and a fake tracker, and the command a parallel child runs)
        self.child_cmd = child_cmd or (lambda n: [sys.executable, str(KIT_DIR / "bin" / "agent-kit"), "run", "--ticket", str(n)])
        self.root = Path(cfg["_root"])
        self.tr = tracker or make(cfg)
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
        """`agent-kit status`: ready, blocked and not-ready (refine) tickets, then runs. No LLM, no GitHub writes."""
        lines, shown, mode = [], set(), self.cfg["loop"]["refine"]
        for c in self.tr.candidates():
            chk = refine.check(c.get("body"), self.cfg["refine"]) if mode != "off" else {"ok": True}
            if c["unmet"]:
                lines.append(f"BLOCKED #{c['number']} {c['title']} waits on {', '.join('#%d' % u for u in c['unmet'])}")
            elif not chk["ok"]:
                shown.add(c["number"])
                lines.append(f"REFINE  #{c['number']} {c['title']}: not ready: {'; '.join(chk['reasons'])}")
            else:
                lines.append(f"READY   #{c['number']} {c['title']}")
        for r in all_runs(self.cfg):
            if not r["status"]:  # refine records only (the ticket never ran)
                note = report.refine_note(r.s)
                if note and r["number"] not in shown:
                    waits = (r["refine"] or {}).get("proposed") and not (r["refine_apply"] or {}).get("done")
                    labelled = f", labelled {self.cfg['tracker']['labels']['refine']}" if waits else ""
                    lines.append(f"REFINE  #{r['number']} {r['title']}: {note}{labelled}")
                continue
            if r["status"] not in ("done",):
                usd = sum((llm.cost_summary(r.cost_log) or {}).get(k, {}).get("usd", 0)
                          for k in (llm.cost_summary(r.cost_log) or {}))
                note = followup.status_note(r.s, self.cfg["loop"])
                held = parallel.holder(r.dir / "lock")
                batch = (r["parallel"] or {}).get("batch") or []
                working = r["status"] in ("queued", IMPLEMENTING) or str(r["status"])[len(FIXING_REVIEW):].isdigit()
                if len(batch) > 1 and working:
                    note = (f"{note}; " if note else "") + "batch " + ", ".join(f"#{x}" for x in batch)
                if held:
                    note = f"locked by pid {held['pid']} since {time.strftime('%H:%M', time.localtime(held['ts']))}" \
                           + (f"; {note}" if note else "")
                lines.append(f"RUN     #{r['number']} {r['key']} status={r['status']} "
                             f"pr={r['pr'] or '-'} cost=${usd:.2f}" + (f" | {note}" if note else ""))
        return lines

    def pick(self, number=None, dry=False):
        """The ticket to do next: `number`, or the lowest ready one with no unmet blockers. Each goes through
        refine() first, except a ticket whose run already exists (resuming). dry: the check only, nothing written."""
        mode = self.cfg["loop"]["refine"]
        mode = "check" if dry and mode == "auto" else mode
        if number:
            i = self.tr.get(number)
            c = {**i, "key": self.tr.key(i), "blocked_by": self.tr.blockers(i)}
            return c if Run(self.cfg, number)["status"] or self.refine(c, mode, record=not dry) else None
        for c in self.tr.candidates():
            if not c["unmet"] and self.refine(c, mode, record=not dry):
                return c
        return None

    # --- refine: is the ticket ready? ----------------------------------------
    def refine(self, issue, mode=None, record=True):
        """The script check (no LLM); on a failure in "auto" mode, one lean LLM proposal posted as a comment and
        the label swapped ready -> refine. At most one proposal per body version (state.json or the kit's
        comment). Returns True when the ticket may be implemented. record=False: check and log only."""
        c, n = self.cfg, issue["number"]
        mode = mode or c["loop"]["refine"]
        if mode == "off":
            return True
        chk = refine.check(issue.get("body"), c["refine"])
        key = issue.get("key") or self.tr.key(issue)
        if chk["ok"]:
            log(f"refine: {key} ready ({chk['criteria']} criteria)")
        else:
            log(f"refine: {key} not ready: {'; '.join(chk['reasons'])}")
        if not record:
            return chk["ok"]
        run, bh = Run(c, n), refine.body_hash(issue.get("body"))
        prev = run["refine"] or {}
        row = {"ts": int(time.time()), "mode": mode, "ok": chk["ok"], "reasons": chk["reasons"],
               "criteria": chk["criteria"], "body_hash": bh, "proposed": False}
        if prev.get("proposed") and prev.get("body_hash") == bh:
            row.update({k: prev[k] for k in ("proposed", "source", "split", "criteria_proposed", "questions") if k in prev})
        run.set(key=key, title=issue["title"], refine=row, refine_checks=(run["refine_checks"] or 0) + 1)
        if chk["ok"]:
            return True
        if mode != "auto":
            log(f"refine: {key} skipped (loop.refine=check: no proposal; `agent-kit refine {n}` makes one)")
        elif row["proposed"]:
            log(f"refine: {key} skipped: a proposal for this body is already posted; edit the issue, "
                f"or `agent-kit refine --apply {n}`")
        else:
            self._propose(run, issue, key, chk, row)
        return False

    def _propose(self, run, issue, key, chk, row):
        """Post one proposal for this body version (or adopt the kit's comment that already holds it) and swap
        the label ready -> refine, so the ticket waits for a human."""
        c, n, bh = self.cfg, issue["number"], row["body_hash"]
        try:
            found = refine.latest(self._trusted_comments(n))
        except ValueError:  # a human broke the JSON of an older proposal: make a new one
            found = None
        if found and found.get("body_hash") == bh:
            prop, row["source"] = found, "comment"
            log(f"refine: {key}: proposal for this body found in an issue comment; not asking again")
        else:
            rf = c["refine"]
            text = prompt("refine", key=key, title=issue["title"], body=issue.get("body") or "",
                          reasons="; ".join(chk["reasons"]), context=self._context(issue, key))
            prop, _ = self.claude(text, cwd=str(self.root), step="refine", model=c["models"]["refine"],
                                  schema=refine.SCHEMA, lean=True, system=prompt("refine-system", max_criteria=rf["max_criteria"]),
                                  tools="", budget=c["budget_usd"]["refine"], timeout=c["loop"]["timeout_seconds"],
                                  cost_log=run.cost_log)
            self.tr.issue_comment(n, refine.render_comment(prop, chk, bh, n))
            row["source"] = "llm"
            log(f"refine: {key}: proposal posted ({len(prop['criteria'])} criteria, {len(prop['split'])} split, "
                f"{len(prop['questions'])} questions)")
        if c["tracker"]["labels"]["refine"] not in self.tr.get(n)["labels"]:
            self.tr.set_status(n, "ready", "refine")  # removes ready only when it is there
        row.update(proposed=True, split=len(prop.get("split") or []), criteria_proposed=len(prop.get("criteria") or []),
                   questions=len(prop.get("questions") or []))
        run.set(refine=row)
        log(f"refine: {key} labelled {c['tracker']['labels']['refine']}; set {c['tracker']['labels']['ready']} again when it is right")

    def refine_apply(self, n):
        """`agent-kit refine --apply <n>`: the latest trusted kit proposal (as a human may have edited it). A split
        creates the issues in order (labelled refine, "Blocked by" from the plan plus the parent's blockers),
        then closes the parent as not planned; else the criteria go into the body. Nothing gets the ready
        label: a human sets it. Progress is in state.json (refine_apply), so a rerun after a crash continues.
        Returns an exit code."""
        c, run, issue = self.cfg, Run(self.cfg, n), self.tr.get(n)
        key, t = self.tr.key(issue), c["tracker"]
        try:
            prop = refine.latest(self._trusted_comments(n))
        except ValueError as e:
            log(f"refine: {key}: {e}; fix the block in the comment and run again")
            return 1
        if not prop:
            log(f"refine: {key}: no proposal from agent-kit in the issue comments; run `agent-kit refine {n}` first")
            return 1
        done = run["refine_apply"] or {}
        if done.get("proposal") != prop.get("body_hash"):
            done = {}  # a newer proposal: start over
        if done.get("done"):
            log(f"refine: {key}: proposal already applied ({done['kind']}"
                + (f": {', '.join('#%d' % x for x in done['created'])}" if done.get("created") else "") + ")")
            return 0
        if prop.get("body_hash") != refine.body_hash(issue.get("body")):
            log(f"refine: {key}: the issue body changed since the proposal; applying it anyway")
        split = prop.get("split") or []
        errs = refine.split_errors(split)
        if split and errs:
            log(f"refine: {key}: cannot split: {'; '.join(errs)}; fix the block in the comment and run again")
            return 1
        row = {"ts": int(time.time()), "kind": "split" if split else "criteria", "created": done.get("created") or [],
               "proposal": prop.get("body_hash"), "commented": bool(done.get("commented")), "done": False}
        if split:
            blockers = self.tr.blockers(issue)
            for item in split[len(row["created"]):]:
                body = refine.split_body(item, row["created"], n, blockers, c["refine"]["criteria_heading"],
                                         t["blocked_by_heading"])
                made = self._split_made(n, item["title"], row["created"])  # created before a crash?
                if made:
                    log(f"refine: {key}: #{made} {item['title']} already exists; reusing it")
                else:
                    made = self.tr.create_issue(item["title"], body, [t["labels"]["refine"]])
                    log(f"refine: {key}: created #{made} {item['title']}")
                row["created"].append(made)
                run.set(refine_apply=row)
            made = ", ".join(f"#{x}" for x in row["created"])
            if not done.get("commented"):
                self.tr.issue_comment(n, f"Split into {made}. Closing this one; set {t['labels']['ready']} on each "
                                         f"part when it is right.\n\n{followup.MARKER}")
                row["commented"] = True
                run.set(refine_apply=row)
            self.tr.remove_label(n, "refine")
            self.tr.close(n, reason="not planned")
            log(f"refine: {key}: split into {made}; closed as not planned")
        elif prop.get("criteria"):
            self.tr.edit_body(n, refine.with_criteria(issue.get("body"), prop["criteria"], c["refine"]["criteria_heading"],
                                                      t["blocked_by_heading"]))
            log(f"refine: {key}: {len(prop['criteria'])} criteria written into the body; set {t['labels']['ready']} "
                "when it is right")
        else:
            log(f"refine: {key}: the proposal has no criteria and no split; answer its questions in the issue")
            return 1
        run.set(key=key, title=issue["title"], refine_apply={**row, "done": True})
        return 0

    def _trusted_comments(self, n):
        """The issue's comments by trusted authors (pr.trusted_associations): only those can hold a proposal."""
        return [x for x in self.tr.issue_comments(n) if x.get("association") in self.cfg["pr"]["trusted_associations"]]

    def _split_made(self, parent, title, created):
        """An open issue an earlier, interrupted --apply already created for this split part, or None."""
        head = f"Split from #{parent}."
        for num, i in sorted(self.tr.issues().items()):
            if num not in created and i["state"] == "OPEN" and i["title"] == title and (i.get("body") or "").startswith(head):
                return num
        return None

    def _context(self, issue, key):
        """The context pack for a ticket from the main checkout's knowledge base."""
        c = self.cfg
        if not (kb.kdir(c, self.root) / "files.jsonl").exists():
            return "(no knowledge base yet)"
        related = [self.tr.key(self.tr.get(b)) for b in issue.get("blocked_by", [])]
        return kb.context(c, self.root, f"{issue['title']}\n{issue['body']}", key=key, related_keys=related)

    # --- workspace ---------------------------------------------------------
    def workspace(self, run, issue):
        w = self.cfg["workspace"]
        branch = run["branch"] or fmt(self.cfg["branch"]["name"], self.cfg,
                                      key=issue["key"].lower().lstrip("#"), slug=slug(issue["title"].split(":", 1)[-1]))
        with self._git_lock():
            sh(["git", "fetch", self.remote, self.base], cwd=self.root)
        if w["mode"] == "worktree":
            ws = Path(fmt(w["dir"], self.cfg)).resolve() / branch
            if not ws.exists():
                ws.parent.mkdir(parents=True, exist_ok=True)
                with self._git_lock():
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
    def _git_lock(self):
        """Parallel runs share the main repo's .git (config, refs): fetch, worktree add and push take turns."""
        return parallel.flock(runs_dir(self.cfg) / "git.lock")

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

    def _impl(self, run, ws, text, step, resume=None, model=None):
        c = self.cfg
        with hooks.watch(c, ws, step):  # print the session's hook calls live
            res, sid = self.claude(
                text, cwd=ws, step=step, model=model or c["models"]["implement"], effort=c["effort"]["implement"],
                schema=IMPL_SCHEMA, tools=c["implement"]["tools"], permission_mode=c["implement"]["permission_mode"],
                allow=self._allow(), budget=c["budget_usd"]["fix" if resume else "implement"], resume=resume,
                persist=True, timeout=c["loop"]["timeout_seconds"], cost_log=run.cost_log,
                stream_log=run.dir / f"{step.replace(':', '-')}.log")
        sessions = (run["sessions"] or []) + [{"step": step.replace(":", "-"), "id": sid or resume}]
        run.set(session=sid or resume, sessions=sessions, impl=res)  # sessions: to match hook log rows
        return res

    def _review(self, run, ws, issue):
        c = self.cfg
        ex = _pathspec_excludes(c["review"]["exclude"])
        rng = f"{self.upstream}...HEAD"
        changed = out(["git", "diff", "--name-only", rng, "--", ".", *ex], cwd=ws).splitlines()
        numstat = out(["git", "diff", "--numstat", rng, "--", ".", *ex], cwd=ws)
        # a binary file shows "-\t-": count it as one line so it is never "zero lines changed"
        lines = sum(int(a) + int(d) if a.isdigit() and d.isdigit() else 1
                    for a, d, *_ in (l.split("\t") for l in numstat.splitlines() if l.count("\t") >= 2))
        hist = run["reviews"] or []
        ran = [rv["plan"]["tier"] for rv in hist if rv.get("plan") and rv["plan"]["run"]]
        plan = review_plan(c, changed, lines, issue["body"], reviewed=max(ran, key=TIERS.index, default=None))
        log(f"review plan: {plan['tier'].upper()}{' ' + plan['model'] if plan['run'] else ''} ({plan['reason']})")
        if not plan["run"]:
            res = {"skipped": plan["reason"], "pass": True, "plan": plan}
            run.set(reviews=hist + [res])
            return res, [], []
        stat = out(["git", "diff", "--stat", rng, "--", ".", *ex], cwd=ws)
        diff = out(["git", "diff", "-U3", rng, "--", ".", *ex], cwd=ws)
        text = prompt("review", key=issue["key"], title=issue["title"], body=issue["body"] or "",
                      conventions=kb.review_rules(c, ws, changed), base=self.upstream, stat=stat,
                      diff=clip(diff, c["review"]["diff_chars"]) + (
                          "\n(diff truncated: Read the remaining files listed in the stat)" if len(diff) > c["review"]["diff_chars"] else ""))
        res, _ = self.claude(text, cwd=ws, step="review", model=plan["model"], effort=c["effort"]["review"],
                            schema=REVIEW_SCHEMA, lean=True, system=review_system(c), tools=c["review"]["tools"],
                            budget=c["budget_usd"]["review"], timeout=c["loop"]["timeout_seconds"],
                            cost_log=run.cost_log, stream_log=run.dir / f"review-{len(run['reviews'] or []) + 1}.log")
        block = [f for f in res["findings"] if f["severity"] in c["review"]["block_on"]]
        unmet = [x for x in res["criteria"] if not x["met"]]
        res["pass"] = not block and not unmet
        res["plan"] = plan
        log(f"review: {'PASS' if res['pass'] else 'FAIL'} ({len(block)} blocking, {len(unmet)} unmet criteria, "
            f"{len(res['findings']) - len(block)} minor)")
        hist = (run["reviews"] or []) + [res]
        run.set(reviews=hist)
        return res, block, unmet

    # --- one ticket --------------------------------------------------------
    def do_ticket(self, issue, dry=False):
        c, n, key = self.cfg, issue["number"], issue["key"]
        run = Run(c, n)
        ctx = self._context(issue, key)
        gate_names = ", ".join(g["name"] for g in map(gates._norm, c["gates"])) or "(none)"
        impl_prompt = prompt("implement", key=key, number=n, title=issue["title"], body=issue["body"] or "",
                             context=ctx, gate_names=gate_names, pr_sections=", ".join(c["pr"]["sections"]))
        if dry:
            print(impl_prompt)
            log(f"dry run: {key} would start; prompt is {len(impl_prompt)} chars (~{len(impl_prompt) // 4} tokens)")
            return True

        with self._ticket_lock(run, key) as busy:
            return False if busy else self._work(run, issue, ctx, impl_prompt)

    @contextlib.contextmanager
    def _ticket_lock(self, run, key):
        """Hold .agent/runs/<n>/lock (with a heartbeat) while working on a ticket or its PR. Yields None when held,
        or the live holder ({pid, ts}) when another run has it (the caller skips). Every event is logged and kept in
        state.json "locks"."""
        lock = run.dir / "lock"
        got = parallel.take(lock)
        if got["stale"] is not None:
            self._lock_event(run, "stale", got["stale"].get("pid"))
            log(f"{key}: stale lock of pid {got['stale'].get('pid')} removed (that run is gone)")
        if not got["taken"]:
            self._lock_event(run, "busy", got["holder"]["pid"])
            log(f"{key}: locked by pid {got['holder']['pid']} (another agent-kit run); skipped")
            yield got["holder"]
            return
        self._lock_event(run, "taken", os.getpid())
        log(f"{key}: lock taken (pid {os.getpid()})")
        try:
            with parallel.hold(lock):
                yield None
        finally:
            parallel.release(lock)
            self._lock_event(run, "released", os.getpid())
            log(f"{key}: lock released")

    def _lock_event(self, run, event, pid):
        run.set(locks=((run["locks"] or []) + [{"ts": int(time.time()), "event": event, "pid": pid}])[-LOCKS_KEPT:])

    def _work(self, run, issue, ctx, impl_prompt):
        c, n, key = self.cfg, issue["number"], issue["key"]
        log(f"=== {key} (#{n}): {issue['title']}")
        run.set(key=key, title=issue["title"], status=IMPLEMENTING)
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

    def _fix(self, run, ws, text, step, rnd, last, kind):
        """A resumed fix round; the last allowed one (rnd == last) may use models.escalate."""
        model = fix_model(self.cfg, rnd, last)
        if model != self.cfg["models"]["implement"]:
            esc = {"step": step, "round": rnd, "model": model, "reason": f"last {kind} round ({rnd} of {last})"}
            run.set(escalations=(run["escalations"] or []) + [esc])
            log(f"escalate: {step} uses {model} ({esc['reason']})")
        self._impl(run, ws, text, step, resume=run["session"], model=model)

    def _tdd(self, run, ws, rv, block):
        """Red/green check; a result that does not prove the change becomes a blocking finding on rv."""
        res = tdd.check(self.cfg, ws, self.upstream, _pathspec_excludes(self.cfg["review"]["exclude"]))
        run.set(tdd=res, tdd_checks=(run["tdd_checks"] or []) + ([res] if res else []))
        if not res:
            return
        log(f"tdd: {res['status'].upper()} ({res['reason']}"
            + (f"; {len(res['files'])} test file(s), gate {res['gate']}" if res["files"] else "") + f", {res['secs']}s)")
        f = tdd.finding(res)
        if f:
            rv["findings"] = (rv.get("findings") or []) + [f]
            rv["pass"] = False
            block.append(f)
            run.set(reviews=(run["reviews"] or [])[:-1] + [rv])

    def _gate_rounds(self, run, ws, key):
        """Gates, then up to loop.max_fix_rounds resumed fix rounds while they fail. Returns the last report."""
        c = self.cfg
        rep = self._gates(ws)
        fix_round = 0
        _keep_gate_failure(run, rep, fix_round)
        while not rep["ok"] and fix_round < c["loop"]["max_fix_rounds"]:
            fix_round += 1
            run.set(fix_rounds=(run["fix_rounds"] or 0) + 1)
            details = gates.render({"results": [r for r in rep["results"] if not r["ok"]]})
            self._fix(run, ws, prompt("fix", key=key, reason="These gates fail:", details=details),
                      f"fix-gates:{fix_round}", fix_round, c["loop"]["max_fix_rounds"], "gate-fix")
            rep = self._gates(ws)
            _keep_gate_failure(run, rep, fix_round)
        run.set(gates=rep)
        return rep

    def _quality_rounds(self, run, ws, issue):
        c, key = self.cfg, issue["key"]
        review_round = 0
        run.set(fix_rounds=run["fix_rounds"] or 0)
        while True:
            rep = self._gate_rounds(run, ws, key)
            if not rep["ok"]:
                return {"reason": "gates still failing after fix rounds", "details": gates.render(rep)}
            self._commit(ws, f"{key}: {issue['title'].split(':', 1)[-1].strip()}" if review_round == 0
                         else f"{key}: address review round {review_round}")
            rv, block, unmet = self._review(run, ws, issue)
            self._tdd(run, ws, rv, block)
            if rv["pass"]:
                return None
            if review_round >= c["loop"]["max_review_rounds"]:
                return {"reason": "review still failing after fix rounds", "details": _findings_md(block, unmet)}
            review_round += 1
            run.set(status=f"{FIXING_REVIEW}{review_round}")
            self._fix(run, ws, prompt("fix", key=key, reason="Code review found problems:",
                                      details=_findings_md(block, unmet)),
                      f"fix-review:{review_round}", review_round, c["loop"]["max_review_rounds"], "review-fix")

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
        with self._git_lock():
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
        lines = [f"- {'pass' if r['ok'] else 'FAIL'}: {r['name']}" for r in (rep or {}).get("results", [])]
        if run["tdd"]:
            lines.append(f"- {tdd.line(run['tdd'])}")
        if lines:
            parts += ["## Gates", "", "\n".join(lines), ""]
        reviews = run["reviews"] or []
        if c["pr"]["include_review"] and reviews and reviews[-1].get("skipped"):
            parts += ["## Automated review", "", f"Automated review skipped: {reviews[-1]['skipped']}"
                      + ("" if reviews[-1].get("pass", True) else "; failed by the red/green check"), ""]
        elif c["pr"]["include_review"] and reviews:
            last = reviews[-1]
            plan = last.get("plan")
            minor = [f for f in last["findings"] if f["severity"] not in c["review"]["block_on"]]
            parts += ["## Automated review", "",
                      f"{'Passed' if last.get('pass') else 'Not passed'} after {len(reviews)} round(s)"
                      + (f", reviewed by {plan['model']} ({plan['reason']})." if plan else ".")]
            parts += [f"- [x] {x['criterion']}" if x["met"] else f"- [ ] {x['criterion']}" for x in last["criteria"]]
            if minor:
                parts += ["", "Minor findings left for you:"] + [f"- `{f['file']}`: {f['issue']}" for f in minor]
            parts.append("")
        if impl.get("concerns"):
            parts += ["## Concerns", "", *[f"- {x}" for x in impl["concerns"]], ""]
        if failure:
            parts += ["## Needs a human", "", failure["reason"], "", failure.get("details", ""), ""]
        return "\n".join(parts).rstrip() + "\n"

    # --- after the PR is open: review comments and CI ---------------------
    def address(self, run):
        """One follow-up pass over a run's open PR: new review comments, then failed CI on the head SHA.
        Every decision is a row in state.json "polls". No LLM call when nothing is new."""
        c = self.cfg
        new = followup.new_comments(self.tr.pr_review_comments(run["pr"]), run["handled_comments"],
                                    c["pr"]["trusted_associations"])
        ci = self.tr.pr_checks(run["pr"])
        dec = followup.ci_decision(ci["checks"], ci["sha"], run["handled_ci"], run["ci_rounds"] or 0,
                                  c["loop"]["max_ci_rounds"])
        note = "nothing new" if not new and dec["action"] in ("none", "handled") else ""
        self._poll(run, comments=len(new), ci=dec["action"], sha=ci["sha"], note=note or dec["reason"])
        pushed = self._triage(run, new) if new else False
        if run["status"] == "needs_human":
            return
        if pushed:  # CI runs again on the new head: next poll
            return
        if dec["action"] == "limit":
            self._needs_human(run, dec["reason"], followup.check_lines(dec["failed"]))
        elif dec["action"] == "fix":  # after a push CI runs again on the new head: next poll
            self._fix_ci(run, ci, dec)

    def follow_up(self):
        """One address() pass per run whose PR is open; needs_human runs wait for you. An error on one PR
        is recorded and logged, and the others are still polled."""
        for r in all_runs(self.cfg):
            if r["status"] in FOLLOWING:
                self._address_safely(r)

    def _address_safely(self, r):
        """address() under the ticket lock, with an error recorded as a poll row; one inside a fix makes the run
        needs_human. A run whose lock another process holds is skipped (a poll row says so)."""
        with self._ticket_lock(r, r["key"]) as busy:
            if busy:
                self._poll(r, comments=0, note=f"skipped: locked by pid {busy['pid']}")
                return False
            return self._address_unlocked(r)

    def _address_unlocked(self, r):
        try:
            self.address(r)
            return True
        except (RuntimeError, llm.LLMError) as e:
            err = str(e)[:500]
            self._poll(r, error=err)
            if str(r["status"]).startswith(FIXING_REVIEW):  # stopped inside a fix: don't leave the guard on
                try:
                    self._needs_human(r, f"follow-up failed: {err[:300]}")
                except RuntimeError:
                    r.set(status="needs_human", needs_human={"ts": int(time.time()), "reason": err[:300]})
            return False

    def address_now(self, number):
        """`agent-kit address <n>`: one pass now. On a needs_human run it is your override: label back to
        in-progress, round limits reset, status pr_open. Returns an exit code."""
        r = Run(self.cfg, number)
        if r["status"] not in (*FOLLOWING, "needs_human") or not r["pr"]:
            log(f"#{number}: no open PR to address (status {r['status'] or 'unknown'})")
            return 1
        if r["status"] == "needs_human":
            self.tr.set_status(number, "needs_human", "wip")
            over = {"ts": int(time.time()), "was": "needs_human", "ci_rounds": r["ci_rounds"],
                    "comment_rounds": r["comment_rounds"]}
            r.set(status="pr_open", ci_rounds=0, comment_rounds=0, overrides=(r["overrides"] or []) + [over],
                  handled_ci=(r["handled_ci"] or [])[:-1])  # the last failure may be retried
            log(f"{r['key']}: needs_human overridden by you: round limits reset for this pass")
        return 0 if self._address_safely(r) else 2

    def _needs_human(self, run, reason, details=""):
        """Stop following this PR: label the issue, say why on the PR, status needs_human (polls skip it)."""
        self.tr.set_status(run["number"], None, "needs_human")
        self.tr.comment(run["pr"], f"## Needs a human\n\n{reason}\n\n{details}".rstrip() + f"\n\n{followup.MARKER}")
        run.set(status="needs_human", needs_human={"ts": int(time.time()), "reason": reason})
        log(f"{run['key']}: needs a human: {reason}")

    def _fix_ci(self, run, ci, dec):
        """Failed checks on the head SHA -> one fix round with their compacted logs. The SHA is handled
        before the fix, so a restart never fixes the same failure twice."""
        rnd = (run["ci_rounds"] or 0) + 1
        run.set(ci_rounds=rnd, handled_ci=(run["handled_ci"] or []) + [ci["sha"]])
        logs = [f"### run {rid}\n```\n{compact(self.tr.failed_log(rid))}\n```" for rid in ci["failed_runs"]]
        details = "\n\n".join(logs) or followup.check_lines(dec["failed"])
        self._follow_fix(run, "ci", f"CI fails on {ci['sha'][:7]} ({', '.join(x['name'] for x in dec['failed'])}):",
                         details, rnd, self.cfg["loop"]["max_ci_rounds"], "fix CI", sha=ci["sha"])

    def _triage(self, run, new):
        """One lean, cheap call decides fix|reply|ignore per comment; reply drafts go to replies.md
        (and to the PR only when pr.post_replies). A comment the call leaves out counts as ignore."""
        c = self.cfg
        text = prompt("triage", key=run["key"], title=run["title"], comments="\n\n".join(map(followup.render, new)))
        res, _ = self.claude(text, cwd=run["ws"], step="triage", model=c["models"]["triage"], schema=TRIAGE_SCHEMA,
                             lean=True, system=prompt("triage-system"), tools="", budget=c["budget_usd"]["triage"],
                             timeout=c["loop"]["timeout_seconds"], cost_log=run.cost_log)
        ts = int(time.time())
        got = {d["id"]: d for d in res["comments"]}
        rows = [{**got.get(x["id"], {"id": x["id"], "action": "ignore", "reason": "no decision from triage",
                                     "reply_draft": ""}), "ts": ts} for x in new]
        for r in rows:
            log(f"{run['key']}: triage #{r['id']}: {r['action']} ({r['reason']})")
        run.set(triage=(run["triage"] or []) + rows)
        by_id = {x["id"]: x for x in new}
        replies = [(by_id[r["id"]], r["reply_draft"]) for r in rows if r["reply_draft"].strip()]
        if replies:
            with (run.dir / "replies.md").open("a") as f:
                f.write("".join(followup.reply_md(x, d) for x, d in replies))
            if c["pr"]["post_replies"]:
                for x, d in replies:
                    self._post_reply(run, x, d)
        done = [r["id"] for r in rows if r["action"] != "fix"]
        run.set(handled_comments=(run["handled_comments"] or []) + done)
        fixes = [by_id[r["id"]] for r in rows if r["action"] == "fix"]
        if fixes and (run["comment_rounds"] or 0) >= c["loop"]["max_comment_rounds"]:
            self._needs_human(run, f"comment fix rounds used: {run['comment_rounds']} of {c['loop']['max_comment_rounds']}",
                              "Comments left to address:\n\n" + "\n\n".join(map(followup.render, fixes)))
        elif fixes:
            rnd = (run["comment_rounds"] or 0) + 1
            run.set(comment_rounds=rnd)
            res = self._follow_fix(run, "comments", "Review comments on its PR ask for these changes:",
                                   "\n\n".join(map(followup.render, fixes)), rnd, c["loop"]["max_comment_rounds"],
                                   "address review", ids=[x["id"] for x in fixes])
            if res == "pushed":
                run.set(handled_comments=run["handled_comments"] + [x["id"] for x in fixes])
            return res == "pushed"
        return False

    def _follow_fix(self, run, kind, reason, details, rnd, last, msg, **what):
        """A fix after the PR is open: resumed session (a fresh one if the resume fails), gates with their fix
        rounds, commit, push. The guard is on while it runs (status fixing-review-<kind>). Returns the result."""
        key, ws, step = run["key"], Path(run["ws"]), f"fix-{kind}:{rnd}"
        row = {"ts": int(time.time()), "kind": kind, "round": rnd, "step": step, "fresh_session": False, **what}
        run.set(status=f"{FIXING_REVIEW}{kind}")
        log(f"{key}: {kind} fix round {rnd} of {last}")
        try:
            self._fix(run, ws, prompt("fix", key=key, reason=reason, details=details), step, rnd, last, f"{kind}-fix")
        except llm.LLMError as e:
            if not run["session"]:
                raise
            log(f"{key}: resuming session {run['session']} failed ({str(e)[:200]}); starting a fresh one")
            row["fresh_session"] = True
            issue = self.tr.get(run["number"])
            intro = (f"Ticket {key}: {issue['title']}\n\n{issue.get('body') or ''}\n\nThe branch already holds the "
                     f"work for it (see `git diff {self.upstream}...HEAD`); its PR is open.")
            run.set(session=None)
            self._fix(run, ws, prompt("fix", key=key, reason=f"{intro}\n\n{reason}", details=details),
                      step, rnd, last, f"{kind}-fix")
        rep = self._gate_rounds(run, ws, key)
        head = out(["git", "rev-parse", "HEAD"], cwd=ws)
        if rep["ok"]:
            self._commit(ws, f"{key}: {msg}")
        if not rep["ok"]:
            row["result"] = "gates failing"
        elif out(["git", "rev-parse", "HEAD"], cwd=ws) == head:
            row["result"] = "no changes"
        else:
            with self._git_lock():
                sh(["git", "push", self.remote, run["branch"]], cwd=ws)
            row["result"], row["commit"] = "pushed", out(["git", "rev-parse", "--short", "HEAD"], cwd=ws)
        run.set(status="pr_open", follow_ups=(run["follow_ups"] or []) + [row])
        log(f"{key}: {step}: {row['result']}" + (f" {row['commit']}" if row.get("commit") else ""))
        if row["result"] == "gates failing":
            self._needs_human(run, f"gates still failing after {step} and its gate fix rounds", gates.render(rep))
        elif row["result"] == "no changes":
            self._needs_human(run, f"{step} changed nothing", details)
        return row["result"]

    def _post_reply(self, run, x, draft):
        if x["kind"] == "inline":
            self.tr.reply(run["pr"], x.get("in_reply_to") or x["id"], f"{draft}\n\n{followup.MARKER}")
        else:
            self.tr.comment(run["pr"], f"{followup.quote(x['body'])}\n\n{draft}\n\n{followup.MARKER}")

    def _poll(self, run, **row):
        row = {"ts": int(time.time()), **row}
        run.set(polls=((run["polls"] or []) + [row])[-POLLS_KEPT:], poll_count=(run["poll_count"] or 0) + 1)
        log(f"{run['key']}: poll: {followup.poll_text(row)}")

    # --- the loop ----------------------------------------------------------
    def run(self, ticket=None, wait=False, max_tickets=None, dry=False):
        done = 0
        while True:
            if not os.environ.get(parallel.CHILD_ENV):  # a parallel child: the parent reconciled
                self.reconcile()
            self.tr.issues(refresh=True)
            pending = self.waiting()
            if not ticket and len(pending) >= self.cfg["merge"]["max_open_prs"] and not dry:
                names = ", ".join(f"{r['key']} {r['pr']}" for r in pending)
                if not wait:
                    log(f"paused: waiting for you to merge {names}. Run again after merging (or use --wait).")
                    return 0
                log(f"waiting for merge: {names} (polling every {self.cfg['merge']['poll_seconds']}s, "
                    "LLM only for new comments or failed CI)")
                self.follow_up()
                time.sleep(self.cfg["merge"]["poll_seconds"])
                continue
            if max_tickets is not None and done >= max_tickets:
                return 0
            n = parallel.slots(self.cfg["loop"]["parallel"], self.cfg["merge"]["max_open_prs"], len(pending))
            n = n if max_tickets is None else min(n, max_tickets - done)
            if not ticket and not dry and n > 1:
                code, count = self._batch(n)
                if not count:
                    log("nothing unblocked to do.")
                    return 0
                done += count
                if code:
                    return code
                continue
            issue = self.pick(ticket, dry=dry)
            if not issue:
                log("nothing unblocked to do.")
                for l in self.status_lines():
                    log("  " + l)
                return 0
            ok = self.do_ticket(issue, dry=dry)
            done += 1
            if dry or ticket or not ok:
                return 0 if ok else 2

    def _batch(self, n):
        """Up to n independent ready tickets at the same time, one `agent-kit run --ticket N` process each.
        This process prepares what the children would race on: the kb index (they only read it) and the
        worktrees (git config writes). Returns (exit code, tickets started)."""
        c = self.cfg
        cands = self.tr.candidates()
        locked = {x["number"]: h for x in cands for h in [parallel.holder(Run(c, x["number"]).dir / "lock")] if h}
        sel = parallel.select(cands, n, locked, ready=self.refine)
        left = "; ".join(f"#{x['number']} {x['why']}" for x in sel["left"])
        if not sel["picked"]:
            log("parallel: nothing to start" + (f" ({left})" if left else ""))
            return 0, 0
        log(f"parallel: {', '.join('#%d' % x for x in sel['picked'])} in {n} slot(s)" + (f"; left out: {left}" if left else ""))
        issues, ts = {x["number"]: x for x in cands}, int(time.time())
        row = {"ts": ts, "batch": sel["picked"], "slots": n, "left": sel["left"]}
        if len(sel["picked"]) == 1:  # nothing to run beside it: this process does it
            num = sel["picked"][0]
            Run(c, num).set(parallel=row)
            return (0 if self.do_ticket(issues[num]) else 2), 1
        if (kb.kdir(c, self.root) / "files.jsonl").exists():
            kb.index(c, self.root).close()
        started, failed = [], False
        for num in sel["picked"]:
            run = Run(c, num)
            try:
                self.workspace(run, issues[num])
            except RuntimeError as e:  # this ticket only; the others still run
                failed = True
                run.set(key=issues[num]["key"], title=issues[num]["title"], parallel={**row, "error": str(e)[:500]})
                log(f"parallel: #{num} not started: worktree setup failed: {str(e)[:300]}")
                continue
            run.set(key=issues[num]["key"], title=issues[num]["title"], status="queued", parallel=row)
            started.append(num)
        try:
            res = parallel.run_children({num: self.child_cmd(num) for num in started}, cwd=str(self.root))
        except KeyboardInterrupt:
            log("parallel: interrupted; the children were stopped (`agent-kit run --ticket N` resumes one)")
            raise
        for num, r in sorted(res.items()):
            run = Run(c, num)
            run.set(parallel={**run["parallel"], **r})
            log(f"parallel: #{num} exited {r['exit']} after {r['secs']}s (status {run['status']})")
        return max(parallel.worst(res), 2 if failed else 0), len(sel["picked"])


def _keep_gate_failure(run, rep, rnd):
    """Every failing gate report goes to state gate_failures for `agent-kit retro`: state gates keeps only the last."""
    if not rep["ok"]:
        failed = [r for r in rep["results"] if not r["ok"]]
        run.set(gate_failures=(run["gate_failures"] or []) + [{"round": rnd, "results": failed}])


def _findings_md(block, unmet):
    lines = [f"- unmet criterion: {x['criterion']} ({x['evidence']})" for x in unmet]
    lines += [f"- [{f['severity']}] `{f['file']}`{':' + str(f['line']) if f.get('line') else ''}: "
              f"{f['issue']} -> fix: {f['fix']}" for f in block]
    return "\n".join(lines)
