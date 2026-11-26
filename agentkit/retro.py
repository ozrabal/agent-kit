"""agent-kit retro: learn from past runs. The script aggregates repeated gate errors, review findings
and permission denials across .agent/runs/<n>/; one lean LLM call (unless --no-llm) turns the
aggregate into proposed rules and gotchas, written to the working tree for a human to commit."""
import json
import posixpath
import re
import time
from pathlib import Path

from . import hooks as hook_log
from . import kb, llm, report
from .config import DEFAULTS, RETRO_COST_LOG, RETRO_LOG, runs_dir, unknown_keys
from .kb import similar
from .llm import cost_by_step
from .loop import prompt
from .util import append_jsonl, clip, error_lines, log, one_line, out, plural, read_json, read_jsonl, slug

SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"},
                           "text": {"type": "string"}},
            "required": ["paths", "summary", "text"]}},
        "gotchas": {"type": "array", "items": {"type": "string"}},
        "config": {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "why": {"type": "string"}},
            "required": ["key", "value", "why"]}},
    },
    "required": ["rules", "gotchas", "config"],
}

# report.collect counts kept on each run's facts
COUNTS = ("usd", "fix_rounds", "review_rounds", "escalated", "edit_blocks", "stop_blocks", "guard_blocks", "gates_blocks",
          "triaged", "comment_fixes", "ci_fixes", "needs_human")

_TMP = re.compile(r"(?<![\w./-])(?:/private)?(?:/var/folders|/tmp)/[^\s'\"`:,;)\]]*")
_QUOTED = re.compile(r"""((?<!\w)'[^'\n]*'(?!\w)|(?<!\w)"[^"\n]*"(?!\w))""")  # an apostrophe is not a quote
_HEX = re.compile(r"\b(?:0[xX][0-9a-fA-F]+|(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,})\b")
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)*(?:ms|s)?(?!\w)(?!\.\d)")


def normalize(line, roots):
    """One error or finding line made comparable across runs: the run's folders stripped to repo paths,
    temp dirs, hex ids and numbers (line/col, durations, counts) replaced; quoted values kept."""
    s = line
    for r in sorted((str(r).rstrip("/") for r in roots if r), key=len, reverse=True):
        s = re.sub(r"(?<![\w./-])" + re.escape(r) + r"(/|(?![\w.-]))", lambda m: "" if m.group(1) else ".", s)
    s = _TMP.sub("<tmp>", s)
    parts = _QUOTED.split(s)
    for i in range(0, len(parts), 2):  # outside quotes only
        parts[i] = _NUM.sub("N", _HEX.sub("<hex>", parts[i]))
    return one_line("".join(parts))


_FAIL = re.compile(r"FAIL (.+) \([\d.]+s\)$")


def _rendered_failures(text):
    """gates.render text (hook notes) -> [(gate name, output)] for its FAIL blocks."""
    found = []
    for line in (text or "").splitlines():
        m = _FAIL.match(line.strip())
        if m:
            found.append((m.group(1), []))
        elif found and line.startswith("    "):
            found[-1][1].append(line[4:])
    return [(name, "\n".join(out)) for name, out in found]


def _gate_errors(state, rows, n, roots):
    """(gate, output) pairs: each recorded failing round (else the final gates report), the error lines
    of a run that crashed, and the BLOCK notes of the run's post-edit and stop hooks and of in-session
    `agent-kit gates` calls."""
    reports = state.get("gate_failures")
    if reports is None:  # runs from before gate_failures was recorded
        reports = [state["gates"]] if state.get("gates") and not state["gates"].get("ok") else []
    pairs = [(r["name"], r.get("output") or "") for rep in reports for r in rep.get("results") or [] if not r.get("ok")]
    failure = state.get("failure") or {}
    if failure and not failure.get("details") and failure.get("reason"):  # an exception, not gates or review
        pairs.append(("run", failure["reason"]))
    for row in rows:
        if row.get("event") in ("post-edit", "stop", "gates") and row.get("outcome") == hook_log.BLOCK:
            pairs += _rendered_failures(row.get("note"))
    return [{"gate": g, "text": normalize(l, roots)} for g, out in pairs for l in error_lines(out, n)]


def _detail(inp, roots):
    """What a denied or blocked tool call wanted: a command's first two words, else its target (normalized)."""
    if inp.get("command"):
        return " ".join(normalize(str(inp["command"]), roots).split()[:2])
    target = inp.get("file_path") or inp.get("path") or inp.get("url") or inp.get("pattern") or ""
    return normalize(str(target), roots)[:120]


def _log_denials(path, roots):
    """Permission denials in one stream log, which holds one session per result event (a step that runs
    again appends): a session's result list, else its permission_denied events, so each counts once."""
    found, events = [], []
    for raw in Path(path).read_text(errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("subtype") == "permission_denied":
            events.append({"source": "claude", "tool": ev.get("tool_name") or "?",
                           "detail": normalize(str(ev.get("message") or ""), roots)[:120]})
        elif ev.get("type") == "result":
            listed = [{"source": "claude", "tool": d.get("tool_name") or "?", "detail": _detail(d.get("tool_input") or {}, roots)}
                      for d in ev.get("permission_denials") or []]
            found += listed or events
            events = []
    return found + events  # a session that ended without a result


def _denials(run_dir, calls, rows, roots):
    """Permission denials from the run's stream logs; calls that report more denials than the logs show
    (lean calls have no log) add one without details each; then the scope guard's blocks."""
    found = [d for f in sorted(Path(run_dir).glob("*.log")) for d in _log_denials(f, roots)]
    missing = sum(c.get("denied") or 0 for c in calls) - len(found)
    found += [{"source": "claude", "tool": "?", "detail": "(no details in the logs)"}] * max(0, missing)
    found += [{"source": "guard", "tool": r.get("tool") or "?", "detail": _detail(r, roots)}
              for r in rows if r.get("event") == "guard" and r.get("outcome") == hook_log.BLOCK]
    return found


def run_facts(run_dir, state, calls, hook_rows, n_lines=3):
    """What one ticket run teaches: gate error lines, review findings and denials (normalized), cost per
    step kind, failed red/green checks. calls: its cost.jsonl rows; hook_rows: all hooks.jsonl rows."""
    run_dir = Path(run_dir)
    # its worktree, and the repo (<repo>/.agent/runs/<n>), as written and with symlinks resolved
    roots = [state.get("ws"), run_dir.absolute().parents[2], run_dir.resolve().parents[2]]
    rows = hook_log.rows_for(hook_rows, state)
    findings = [{"severity": f.get("severity") or "?", "file": f.get("file") or "-",
                 "text": normalize(str(f.get("issue") or ""), roots)}
                for rv in state.get("reviews") or [] for f in rv.get("findings") or []]
    steps = {k: {**v, "usd": round(v["usd"], 4), "secs": round(v["secs"], 1)} for k, v in cost_by_step(calls).items()}
    return {"ticket": int(run_dir.name), "key": state.get("key") or f"#{run_dir.name}",
            "errors": _gate_errors(state, rows, n_lines, roots), "findings": findings,
            "denials": _denials(run_dir, calls, rows, roots), "steps": steps,
            "tdd_failed": sum(1 for t in state.get("tdd_checks") or [] if t.get("status") in ("passes_on_base", "no_tests"))}


def collect(runs_dir, since=None, n_lines=3):
    """run_facts for each ticket run report.collect selects (since: unix ts; older cost and hook rows
    are ignored, runs with no newer LLM call left out), with its report counts under "counts"."""
    runs_dir = Path(runs_dir)
    hook_rows = report.newer(read_jsonl(runs_dir / hook_log.LOG_NAME), since)
    facts = []
    for row in report.collect(runs_dir, since=since)["rows"]:
        d = runs_dir / str(row["ticket"])
        calls = report.newer(read_jsonl(d / "cost.jsonl"), since)
        f = run_facts(d, read_json(d / "state.json", {}) or {}, calls, hook_rows, n_lines)
        f["counts"] = {k: row.get(k) or 0 for k in COUNTS}
        facts.append(f)
    return facts


def _groups(facts, field, keys, extra=None):
    """Items of facts[field] grouped by their keys fields: {**keys, runs: [tickets], count}, most runs
    first, then most occurrences, then first seen. extra(group, item) adds per-group details."""
    groups = {}
    for f in facts:
        for x in f[field]:
            g = groups.setdefault(tuple(x[k] for k in keys), {**{k: x[k] for k in keys}, "runs": [], "count": 0})
            if f["ticket"] not in g["runs"]:
                g["runs"].append(f["ticket"])
            g["count"] += 1
            if extra:
                extra(g, x)
    return sorted(groups.values(), key=lambda g: (-len(g["runs"]), -g["count"]))


def _add_finding(g, x):
    g["severities"] = sorted(set(g.get("severities", [])) | {x["severity"]})
    g["files"] = sorted(set(g.get("files", [])) | {x["file"]})


def aggregate(facts, min_runs, top):
    """The cross-run picture from collect(): gate errors and review findings seen in >= min_runs distinct
    runs (top of each; "single" counts the groups below the bar), the files and folders findings point at,
    denied or guard-blocked tool calls, LLM steps by cost, and summed report counts."""
    def split(groups):
        rep = [g for g in groups if len(g["runs"]) >= min_runs]
        return rep[:top], len(groups) - len(rep)

    errors, single_errors = split(_groups(facts, "errors", ("gate", "text")))
    findings, single_findings = split(_groups(facts, "findings", ("text",), _add_finding))
    located = [{**f, "findings": [{**x, "dir": posixpath.dirname(x["file"]) or "."} for x in f["findings"]
                                  if x["file"] not in ("", "-")]} for f in facts]
    files = _groups(located, "findings", ("file",))[:top]
    dirs = _groups(located, "findings", ("dir",))[:top]
    steps = {}
    for f in facts:
        for k, v in f["steps"].items():
            s = steps.setdefault(k, {"step": k, "calls": 0, "usd": 0.0, "secs": 0.0, "runs": 0})
            s["calls"] += v["calls"]
            s["usd"] += v["usd"]
            s["secs"] += v["secs"]
            s["runs"] += 1
    steps = [{**s, "usd": round(s["usd"], 4), "secs": round(s["secs"], 1)}
             for s in sorted(steps.values(), key=lambda s: -s["usd"])][:top]
    counts = {}
    for f in facts:
        for k, v in f["counts"].items():
            counts[k] = round(counts.get(k, 0) + (v or 0), 4)
    counts["tdd_failed"] = sum(f["tdd_failed"] for f in facts)
    return {"runs": len(facts), "min_runs": min_runs, "errors": errors, "findings": findings, "files": files,
            "dirs": dirs, "denials": _groups(facts, "denials", ("source", "tool", "detail"))[:top],
            "steps": steps, "counts": counts, "single": {"errors": single_errors, "findings": single_findings}}


def repeated_denials(agg):
    return [d for d in agg["denials"] if len(d["runs"]) >= agg["min_runs"]]


def repeated(agg):
    """True when something showed up in >= min_runs runs: worth one LLM call to turn into rules."""
    return bool(agg["errors"] or agg["findings"] or repeated_denials(agg))


def _seen(g):
    return f"{plural(len(g['runs']), 'run')} ({', '.join('#%d' % t for t in g['runs'])}), {plural(g['count'], 'time')}"


def _section(title, lines):
    return ["", f"## {title}", *(lines or ["(none)"])]


def render(agg):
    """The printed retro report: the same text the LLM gets as its aggregate."""
    if not agg["runs"]:
        return "retro: no runs"
    c, bar = agg["counts"], agg["min_runs"]
    out = [f"retro: {plural(agg['runs'], 'run')}, ${c.get('usd', 0):.2f}, fix rounds {c.get('fix_rounds', 0)}, "
           f"review rounds {c.get('review_rounds', 0)}, escalated {c.get('escalated', 0)}, "
           f"red/green failed {c.get('tdd_failed', 0)}",
           f"hook blocks: edit {c.get('edit_blocks', 0)}, stop {c.get('stop_blocks', 0)}, guard {c.get('guard_blocks', 0)}, "
           f"in-session gates {c.get('gates_blocks', 0)}; "
           f"after the PR: triaged {c.get('triaged', 0)}, comment fixes {c.get('comment_fixes', 0)}, "
           f"CI fixes {c.get('ci_fixes', 0)}, needs a human {c.get('needs_human', 0)}"]
    out += _section("Most expensive steps", [f"- {s['step']}: ${s['usd']:.2f}, {plural(s['calls'], 'call')} in "
                                             f"{plural(s['runs'], 'run')}, {s['secs']}s" for s in agg["steps"]])
    errors = [f"- [{e['gate']}] {e['text']}: {_seen(e)}" for e in agg["errors"]]
    if agg["single"]["errors"]:
        errors = (errors or ["(none)"]) + [f"{agg['single']['errors']} more seen in one run only"]
    out += _section(f"Repeated gate errors (in >= {bar} runs)", errors)
    findings = [f"- {f['text']}: {_seen(f)}; {', '.join(f['severities'])}; {', '.join(f['files'])}"
                for f in agg["findings"]]
    if agg["single"]["findings"]:
        findings = (findings or ["(none)"]) + [f"{agg['single']['findings']} more seen in one run only"]
    out += _section(f"Repeated review findings (in >= {bar} runs)", findings)
    out += _section("Files with findings", [f"- {f['file']}: {_seen(f)}" for f in agg["files"]])
    out += _section("Folders with findings", [f"- {f['dir']}: {_seen(f)}" for f in agg["dirs"]])
    def why(d):
        if d["source"] == "guard":
            return "scope guard"
        return "permission: candidate for implement.allow_bash" if d["tool"] == "Bash" else "permission"
    out += _section("Denied tool calls", [f"- {d['tool']} `{d['detail']}`: {_seen(d)} ({why(d)})" for d in agg["denials"]])
    return "\n".join(out)


def llm_input(aggregate_text, known, cfg):
    """(system, prompt) for the one lean retro call: fixed instructions as the system prompt; the
    rendered report (capped at retro.max_chars) and the known rules and gotchas (capped) as the prompt."""
    return prompt("retro-system"), prompt("retro", aggregate=clip(aggregate_text, cfg["retro"]["max_chars"]),
                                          known=clip(known, cfg["retro"]["known_chars"]) or "(none recorded)")


def rule_text(rule):
    """A proposed rule as a .knowledge/rules/*.md file (paths and summary in the frontmatter)."""
    return (f"---\npaths: {json.dumps(list(rule.get('paths') or []))}\nsummary: {json.dumps(one_line(rule.get('summary')), ensure_ascii=False)}\n"
            f"---\n\n{str(rule.get('text') or '').strip()}\n")


def plan_rules(proposed, existing):
    """Which proposed rules to write: ([(file stem, rule)], [(summary, why skipped)]). Never overwrites:
    a taken name (retro-<slug of the summary>) or a summary like an existing rule's with the same paths is skipped."""
    write, skipped, names = [], [], {r["name"] for r in existing}
    for r in proposed:
        summary = one_line(r.get("summary"))
        if not summary or not str(r.get("text") or "").strip():
            skipped.append((summary, "empty"))
            continue
        full = slug(summary, 200)
        name = "retro-" + (full if len(full) <= 40 else full[:41].rsplit("-", 1)[0])  # cut at a word
        dup = next((e for e in existing if sorted(e["paths"]) == sorted(r.get("paths") or [])
                     and similar(summary, str(e["summary"]))), None)
        if name in names:
            skipped.append((summary, f"{name}.md exists"))
        elif dup:
            skipped.append((summary, f"like {dup['name']}.md"))
        else:
            names.add(name)
            write.append((name, {**r, "summary": summary}))
    return write, skipped


def _nested(key):
    """"a.b.c" -> {"a": {"b": {"c": None}}}, for config.unknown_keys."""
    tree = None
    for part in reversed(key.split(".")):
        tree = {part: tree}
    return tree


def config_lines(suggestions, defaults):
    """The LLM's config suggestions as lines to print (never written); keys DEFAULTS does not know are marked."""
    return [f"- {s.get('key') or ''} = {s.get('value')}: {s.get('why')}"
            + ("" if s.get("key") and not unknown_keys(_nested(s["key"]), defaults) else " (unknown key)")
            for s in suggestions]


def _known_lessons(cfg, cwd):
    """The rules, gotchas and conventions the knowledge base already has, one line each."""
    lines = [f"- {r['summary']} ({', '.join(r['paths']) or 'everywhere'})" for r in kb.load_rules(cfg, cwd)]
    for name in ("gotchas.md", "conventions.md"):
        f = kb.kdir(cfg, cwd) / name
        if f.exists():
            lines += [l for l in f.read_text().splitlines() if l.startswith("- ")]
    return "\n".join(lines)


def _kb_changed(cfg, cwd):
    """Uncommitted changes (staged or not) to tracked files under the knowledge base folder."""
    return out(["git", "diff", "HEAD", "--name-only", "--", cfg["kb"]["dir"]], cwd=cwd)


def _untracked(cwd, paths):
    """Those of paths (repo-relative) that git does not track."""
    if not paths:
        return []
    found = out(["git", "status", "--porcelain", "--untracked-files=all", "--", *paths], cwd=cwd)
    return [l[3:] for l in found.splitlines() if l.startswith("??")]


def _write(cfg, cwd, res):
    """Proposed rules and gotchas into the working tree (never committed). Returns (lines, row fields)."""
    d, kd = kb.kdir(cfg, cwd), cfg["kb"]["dir"]
    write, skipped = plan_rules(res.get("rules") or [], kb.load_rules(cfg, cwd))
    lines, touched = ["", "## Proposed rules"], [f"{kd}/rules/{name}.md" for name, _ in write]
    for name, r in write:
        (d / "rules").mkdir(parents=True, exist_ok=True)
        (d / "rules" / f"{name}.md").write_text(rule_text(r))
        lines.append(f"- wrote {kd}/rules/{name}.md: {r['summary']} ({', '.join(r.get('paths') or []) or 'everywhere'})")
    lines += [f"- skipped {summary}: {why}" for summary, why in skipped]
    if write:
        kb.write_digest(cfg, cwd)
        touched.append(f"{kd}/rules-digest.md")
    gotchas = [g for g in res.get("gotchas") or [] if str(g).strip()]
    added = kb.add_lessons(cfg, cwd, [("gotchas", g, "retro") for g in gotchas]) if gotchas else 0
    if added:
        touched.append(f"{kd}/gotchas.md")
    lines += [f"gotchas: {added} added, {len(gotchas) - added} already known"]
    lines += ["", "## Config suggestions (not applied)", *(config_lines(res.get("config") or [], DEFAULTS) or ["(none)"])]
    stat = out(["git", "diff", "--stat", "--", kd], cwd=cwd)
    lines += ["", "## Changes (review, then commit them yourself)", *([stat] if stat else []),
              *[f"untracked: {p}" for p in _untracked(cwd, touched)]]
    return lines, {"rules": [n for n, _ in write], "rules_skipped": len(skipped), "gotchas": added,
                   "config": len(res.get("config") or [])}


def scan(cfg, since=None):
    """The aggregate of the runs in .agent/runs/ (since: unix ts), with the retro.* settings."""
    rc = cfg["retro"]
    return aggregate(collect(runs_dir(cfg), since, rc["error_lines"]), rc["min_runs"], rc["top"])


def run(cfg, cwd, since=None, use_llm=True, claude=None):
    """agent-kit retro: print the aggregate; unless use_llm is False or nothing repeats, one lean LLM call
    proposes rules and gotchas, written to the working tree. Every run appends a row to .agent/runs/retro.jsonl.
    Returns (exit code, text to print)."""
    runs, agg = runs_dir(cfg), scan(cfg, since)
    report_text = render(agg)
    row = {"ts": int(time.time()), "since": since, "runs": agg["runs"], "errors": len(agg["errors"]),
           "findings": len(agg["findings"]), "denials": len(repeated_denials(agg)),
           "usd": 0, "rules": [], "rules_skipped": 0, "gotchas": 0, "config": 0}

    def done(code, mode, note, lines=()):
        append_jsonl(runs / RETRO_LOG, {**row, "mode": mode})
        log(f"retro: {agg['runs']} runs, {row['errors']} repeated errors, {row['findings']} repeated findings, "
            f"{row['denials']} repeated denials; {note}")
        return code, "\n".join([report_text, *lines, "", note])

    if not use_llm:
        return done(0, "no-llm", "--no-llm: nothing proposed, nothing written")
    if not repeated(agg):
        return done(0, "nothing", f"nothing repeated in >= {agg['min_runs']} runs: no LLM call")
    if _kb_changed(cfg, cwd):
        return done(1, "refused", f"{cfg['kb']['dir']} has uncommitted changes: commit or stash them first, "
                                  "so the proposal is the only diff (no LLM call)")
    system, text = llm_input(report_text, _known_lessons(cfg, cwd), cfg)
    cost_log = runs / RETRO_COST_LOG
    seen = len(read_jsonl(cost_log))
    res = failed = None
    try:
        res, _ = (claude or llm.claude)(text, cwd=cwd, step="retro", model=cfg["models"]["retro"],
                                        effort=cfg["effort"]["retro"], schema=SCHEMA, lean=True, system=system,
                                        tools="", budget=cfg["budget_usd"]["retro"], cost_log=cost_log)
    except llm.LLMError as e:
        failed = e
    row["usd"] = round(sum(c.get("usd") or 0 for c in read_jsonl(cost_log)[seen:]), 4)  # also a failed call's
    if failed:
        return done(1, "error", f"LLM call failed, nothing written: {failed}")
    lines, wrote = _write(cfg, cwd, res)
    row.update(wrote)
    return done(0, "llm", f"LLM ${row['usd']}: {len(wrote['rules'])} rules written ({wrote['rules_skipped']} skipped), "
                          f"{wrote['gotchas']} gotchas added, {wrote['config']} config suggestions", lines)
