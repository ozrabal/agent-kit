"""Issue tracker adapter. Only GitHub for now; the loop talks to this interface:
issues(), get(n), key(issue), blockers(issue), is_done(issue), candidates(),
set_status(n, old, new), close(n), find_pr(branch), create_pr(...), pr_state(ref),
pr_review_comments(pr), pr_checks(pr), failed_log(run_id), reply(pr, comment_id, body), comment(pr, body).
"""
import json
import re

from .util import out, sh


class GitHub:
    def __init__(self, cfg, run=sh):
        self.cfg = cfg
        self.run = run  # tests pass a fake gh runner
        self.t = cfg["tracker"]
        self.root = cfg["_root"]
        self._cache = None

    def _gh(self, *args, check=True):
        return self.run(["gh", *args], cwd=self.root, check=check)

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

    # --- PR follow-up: review comments and CI --------------------------------
    def _pages(self, endpoint):
        pages = json.loads(self._gh("api", "--paginate", "--slurp", endpoint).stdout or "[]")
        return [x for page in pages for x in page]

    def pr_review_comments(self, pr):
        """Inline review comments, review bodies and PR conversation comments, one shape:
        {id, kind: inline|review|issue, author, path, line, body, created_at, resolved, diff_hunk, in_reply_to}."""
        n = pr_number(pr)
        q = ("query($owner:String!,$name:String!,$n:Int!,$endCursor:String){repository(owner:$owner,name:$name)"
             "{pullRequest(number:$n){reviewThreads(first:100,after:$endCursor){nodes{isResolved "
             "comments(first:100){nodes{databaseId}}} pageInfo{hasNextPage endCursor}}}}}")
        pages = json.loads(self._gh("api", "graphql", "--paginate", "--slurp", "-F", "owner={owner}", "-F", "name={repo}",
                                    "-F", f"n={n}", "-f", f"query={q}").stdout)
        threads = [t for p in pages for t in p["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]]
        resolved = {c["databaseId"] for t in threads if t["isResolved"] for c in t["comments"]["nodes"]}

        def one(x, kind, created, path=None, line=None, hunk=None, reply_to=None):
            return {"id": x["id"], "kind": kind, "author": (x.get("user") or {}).get("login"), "path": path,
                    "line": line, "body": x.get("body") or "", "created_at": created, "resolved": x["id"] in resolved,
                    "diff_hunk": hunk, "in_reply_to": reply_to, "association": x.get("author_association")}

        res = [one(x, "inline", x["created_at"], x["path"], x.get("line") or x.get("original_line"),
                   x.get("diff_hunk"), x.get("in_reply_to_id"))
               for x in self._pages(f"repos/{{owner}}/{{repo}}/pulls/{n}/comments")]
        res += [one(x, "review", x.get("submitted_at")) for x in self._pages(f"repos/{{owner}}/{{repo}}/pulls/{n}/reviews")]
        res += [one(x, "issue", x["created_at"]) for x in self._pages(f"repos/{{owner}}/{{repo}}/issues/{n}/comments")]
        return res

    def pr_checks(self, pr):
        """{"sha": head commit, "checks": [{name, bucket, state, link}], "failed_runs": [Actions run ids]}.
        bucket is pass|fail|pending|skipping|cancel; gh exits 8 (pending) or 1 (failed / no checks)."""
        n = str(pr_number(pr))
        sha = json.loads(self._gh("pr", "view", n, "--json", "headRefOid").stdout)["headRefOid"]
        raw = self._gh("pr", "checks", n, "--json", "name,bucket,state,link", check=False).stdout.strip()
        checks = json.loads(raw) if raw.startswith("[") else []
        runs = [int(m.group(1)) for c in checks if c.get("bucket") == "fail"
                for m in [re.search(r"/actions/runs/(\d+)", c.get("link") or "")] if m]
        return {"sha": sha, "checks": checks, "failed_runs": list(dict.fromkeys(runs))}

    def failed_log(self, run_id):
        return self._gh("run", "view", str(run_id), "--log-failed", check=False).stdout

    def reply(self, pr, comment_id, body):
        """Reply in an inline comment's thread."""
        self._gh("api", "-X", "POST", f"repos/{{owner}}/{{repo}}/pulls/{pr_number(pr)}/comments/{comment_id}/replies",
                 "-f", f"body={body}")

    def comment(self, pr, body):
        self._gh("pr", "comment", str(pr_number(pr)), "--body", body)


def pr_number(ref):
    """42 from 42, "42" or a PR URL ending in /pull/42."""
    return int(str(ref).rstrip("/").rsplit("/", 1)[-1])


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
