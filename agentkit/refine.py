"""Is a ticket ready to implement? The script check before implement, and the LLM proposal's
comment and --apply plan. Pure functions; the loop (loop.py) calls the tracker and the LLM around them."""
import hashlib
import json
import re

from .followup import MARKER

FENCE = "json agent-kit-refine"  # info string of the proposal block that --apply reads back

_CRITERIA = {"type": "array", "items": {"type": "string"}}
SCHEMA = {  # the LLM proposal when the check fails (loop.refine "auto" or `agent-kit refine <n>`)
    "type": "object",
    "properties": {
        "criteria": _CRITERIA,
        "split": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "criteria": _CRITERIA,
                           "blocked_by": {"type": "array", "items": {"type": "integer"}}},
            "required": ["title", "body", "criteria", "blocked_by"]}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["criteria", "split", "questions"],
}

_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)")


def _lines(body):
    """(offset, line, in_fence) for each line; fence lines themselves count as fenced."""
    pos, fence = 0, False
    for line in (body or "").splitlines(keepends=True):
        mark = line.lstrip().startswith(("```", "~~~"))
        if mark:
            fence = not fence
        yield pos, line, fence or mark
        pos += len(line)


def _section(body, heading):
    """(start, content_start, end) of the first section under a heading named `heading` (any level,
    case-insensitive), up to the next heading of the same or a higher level outside code fences; or None."""
    head = re.compile(r"^(#{1,6})\s*" + re.escape(heading) + r"\b", re.I)
    found = None
    for pos, line, fenced in _lines(body):
        m = None if fenced else re.match(r"^(#{1,6})\s", line)
        if found is None:
            h = None if fenced else head.match(line)
            if h:
                found = (pos, pos + len(line), len(h.group(1)))
        elif m and len(m.group(1)) <= found[2]:
            return found[0], found[1], pos
    return (found[0], found[1], len(body)) if found else None


def criteria(body, heading="acceptance criteria"):
    """Bullet, numbered or checkbox items in the section under `heading` (sub-headings included, code fences
    skipped). None when there is no such heading."""
    sec = _section(body or "", heading)
    if not sec:
        return None
    part = body[sec[1]:sec[2]]
    return [m.group(1) for _, line, fenced in _lines(part) if not fenced for m in [_ITEM.match(line)] if m]


def check(body, rc):
    """The script check (no LLM): {"ok", "reasons": [why it fails], "criteria": count}. rc = cfg["refine"]."""
    heading, body = rc["criteria_heading"], body or ""
    items = criteria(body, heading)
    reasons = []
    if items is None:
        reasons.append(f"no '{heading}' section")
    elif not items:
        reasons.append(f"'{heading}' section has no bullet or checkbox items")
    if rc["max_chars"] and len(body) > rc["max_chars"]:
        reasons.append(f"body is {len(body)} chars > refine.max_chars ({rc['max_chars']})")
    if items and len(items) > rc["max_criteria"]:
        reasons.append(f"{len(items)} criteria > refine.max_criteria ({rc['max_criteria']}): too big")
    return {"ok": not reasons, "reasons": reasons, "criteria": len(items or [])}


def body_hash(body):
    """Short hash of an issue body: a proposal is made once per body version."""
    return hashlib.sha1((body or "").strip().encode()).hexdigest()[:12]


def _title(heading):
    return heading[:1].upper() + heading[1:]


def render_comment(proposal, chk, bhash, number):
    """The proposal as an issue comment: readable markdown, then the JSON block --apply reads (a human may edit it)."""
    p = proposal
    parts = ["## Refinement proposal", "", "This ticket was skipped: " + "; ".join(chk["reasons"]) + "."]
    if p.get("criteria"):
        parts += ["", "### Acceptance criteria", "", *[f"- [ ] {c}" for c in p["criteria"]]]
    if p.get("split"):
        parts += ["", "### Split", ""]
        for i, s in enumerate(p["split"], 1):
            dep = f" (blocked by {', '.join(map(str, s['blocked_by']))})" if s.get("blocked_by") else ""
            parts += [f"{i}. {s['title']}{dep}", *[f"   - [ ] {c}" for c in s.get("criteria") or []]]
    if p.get("questions"):
        parts += ["", "### Questions", "", *[f"- {q}" for q in p["questions"]]]
    block = json.dumps({**p, "body_hash": bhash}, indent=2, ensure_ascii=False)
    parts += ["", f"Edit the block below if needed, then run `agent-kit refine --apply {number}` "
                  "(a split creates the issues and closes this one; otherwise the criteria go into the body). "
                  "Set the ready label again when it is right.", "", f"```{FENCE}", block, "```", "", MARKER]
    return "\n".join(parts) + "\n"


def parse_comment(text):
    """The proposal in a kit comment, None when it has none. ValueError when an edited block is not valid JSON."""
    m = re.search(r"```" + re.escape(FENCE) + r"\n(.*?)\n```", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"the proposal block is not valid JSON: {e}")


def latest(comments):
    """The last proposal the kit posted (MARKER present), or None."""
    for c in reversed(comments):
        if MARKER in (c.get("body") or ""):
            p = parse_comment(c["body"])
            if p is not None:
                return p
    return None


def with_criteria(body, items, criteria_heading, blocked_by_heading):
    """body with an acceptance criteria section holding items: replaces an existing one, else goes before
    the Blocked by section, else at the end."""
    body = (body or "").rstrip() + "\n"
    block = f"## {_title(criteria_heading)}\n\n" + "".join(f"- [ ] {c}\n" for c in items)
    sec = _section(body, criteria_heading)
    if sec:
        return body[: sec[0]] + block + ("\n" + body[sec[2]:] if sec[2] < len(body) else "")
    sec = _section(body, blocked_by_heading)
    if sec:
        return body[: sec[0]] + block + "\n" + body[sec[0]:]
    return body + "\n" + block


def split_errors(split):
    """What is wrong with a split plan before any issue is created; [] when it can be applied."""
    errs = []
    for i, s in enumerate(split, 1):
        if not (s.get("title") or "").strip():
            errs.append(f"split {i}: no title")
        for b in s.get("blocked_by") or []:
            if b == i:
                errs.append(f"split {i}: blocked by itself")
            elif not isinstance(b, int) or b < 1:
                errs.append(f"split {i}: blocked by {b!r}, not a split number (1-based)")
            elif b > i:
                errs.append(f"split {i}: blocked by {b}, which comes later")
        if not s.get("criteria"):
            errs.append(f"split {i}: no criteria")
    return errs


def split_body(item, created, parent, parent_blockers, criteria_heading, blocked_by_heading):
    """A split issue's body. created: numbers of the split issues made so far, in order (blocked_by indexes them);
    the parent's own blockers are inherited."""
    nums = sorted(set(parent_blockers) | {created[b - 1] for b in item.get("blocked_by") or []})
    deps = "".join(f"- #{n}\n" for n in nums) or "None (can start immediately)\n"
    return (f"Split from #{parent}.\n\n{(item.get('body') or '').strip()}\n\n## {_title(criteria_heading)}\n\n"
            + "".join(f"- [ ] {c}\n" for c in item["criteria"]) + f"\n## {_title(blocked_by_heading)}\n\n{deps}")
