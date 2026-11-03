"""Issue tracker adapter. Only GitHub for now; the loop talks to this interface:
issues(), get(n), key(issue), blockers(issue), is_done(issue), candidates(),
set_status(n, old, new), close(n), find_pr(branch), create_pr(...), pr_state(ref).
"""
import json
import re

from .util import out, sh


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.t = cfg["tracker"]
        self.root = cfg["_root"]
        self._cache = None

    def _gh(self, *args, check=True):
        return sh(["gh", *args], cwd=self.root, check=check)

    # --- issues -----------------------------------------------------------
    def issues(self, refresh=False):
        if self._cache is None or refresh:
            raw = self._gh("issue", "list", "--state", "all", "--limit", "1000",
                           "--json", "number,title,labels,state,body").stdout
            self._cache = {}
            for i in json.loads(raw):
                i["labels"] = [l["name"] for l in i["labels"]]
                self._cache[i["number"]] = i
        return self._cache

    def get(self, n):
        i = self.issues().get(n)
        if i is None:
            raw = self._gh("issue", "view", str(n), "--json", "number,title,labels,state,body").stdout
            i = json.loads(raw)
            i["labels"] = [l["name"] for l in i["labels"]]
            self._cache[n] = i
        return i

    def key(self, issue):
        m = re.match(self.t["key_pattern"], issue["title"])
        return m.group(1) if m else f"#{issue['number']}"

    def blockers(self, issue):
        body = issue.get("body") or ""
        head = re.compile(r"(?im)^#{1,6}\s*" + re.escape(self.t["blocked_by_heading"]) + r"\b.*$")
        m = head.search(body)
        if not m:
            return []
        rest = body[m.end():]
        nxt = re.search(r"(?m)^#{1,6}\s", rest)
        section = rest[: nxt.start()] if nxt else rest
        nums = re.findall(r"(?:#|/issues/)(\d+)\b", section)
        return sorted({int(x) for x in nums if int(x) != issue["number"]})

    def is_done(self, issue):
        if self.t["labels"]["done"] in issue["labels"]:
            return True
        return self.t["closed_counts_as_done"] and issue["state"] == "CLOSED"

    def candidates(self):
        """Ready, open issues in number order, each annotated with unmet blockers."""
        ready = self.t["labels"]["ready"]
        res = []
        for n in sorted(self.issues()):
            i = self.issues()[n]
            if i["state"] != "OPEN" or ready not in i["labels"]:
                continue
            bl = self.blockers(i)
            unmet = [b for b in bl if not self.is_done(self.get(b))]
            res.append({**i, "key": self.key(i), "blocked_by": bl, "unmet": unmet})
        return res

    def wip(self):
        lab = self.t["labels"]["wip"]
        return [i for i in self.issues().values() if lab in i["labels"]]

    def set_status(self, n, old, new):
        labels = self.t["labels"]
        args = ["issue", "edit", str(n), "--add-label", labels[new]]
        if old and labels[old] in self.get(n)["labels"]:
            args += ["--remove-label", labels[old]]
        self._gh(*args)
        i = self.get(n)
        i["labels"] = [l for l in i["labels"] if not old or l != labels[old]] + [labels[new]]

    def close(self, n):
        if self.get(n)["state"] == "OPEN":
            self._gh("issue", "close", str(n), "--reason", "completed", check=False)

    # --- pull requests ----------------------------------------------------
    def find_pr(self, branch):
        raw = self._gh("pr", "list", "--head", branch, "--state", "all",
                       "--json", "number,state,url,isDraft").stdout
        prs = json.loads(raw)
        return prs[0] if prs else None

    def pr_state(self, ref):
        raw = self._gh("pr", "view", str(ref), "--json", "number,state,url,isDraft").stdout
        return json.loads(raw)

    def create_pr(self, base, head, title, body_file, draft=False):
        args = ["pr", "create", "--base", base, "--head", head, "--title", title, "--body-file", str(body_file)]
        if draft:
            args.append("--draft")
        url = self._gh(*args).stdout.strip().splitlines()[-1]
        return url

    def edit_pr_body(self, ref, body_file):
        self._gh("pr", "edit", str(ref), "--body-file", str(body_file))


def make(cfg):
    kind = cfg["tracker"]["type"]
    if kind == "github":
        return GitHub(cfg)
    raise SystemExit(f"tracker type '{kind}' is not supported yet (add an adapter in agentkit/tracker.py)")


def default_branch(cwd):
    try:
        ref = out(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
        return ref.rsplit("/", 1)[-1]
    except RuntimeError:
        return "main"
