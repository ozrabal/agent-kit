"""Blocking questions (loop.questions, opt-in): the implementer may stop and ask; the human answers on the issue.
Pure functions; the loop (loop.py) calls the tracker and the LLM around them."""

ITEM = {"type": "object",
        "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                       "recommended": {"type": "string"}, "why": {"type": "string"}},
        "required": ["question", "options", "recommended", "why"]}


def marker(n, rnd):
    return f"<!-- agent-kit questions #{n} round {rnd} -->"


def comment(key, n, rnd, items):
    """The issue comment that asks: numbered questions with lettered options and the recommendation."""
    parts = [f"**{key}: the agent needs your decision before it goes on** (round {rnd})", ""]
    for i, q in enumerate(items, 1):
        parts += [f"### {i}. {q['question'].strip()}", ""]
        parts += [f"- **{chr(97 + j)})** {o.strip()}" for j, o in enumerate(q.get("options") or [])]
        if q.get("recommended"):
            parts += ["", f"Recommended: {q['recommended'].strip()}" + (f" ({q['why'].strip()})" if q.get("why") else "")]
        parts.append("")
    parts += ["Answer in one comment on this issue (e.g. `1: b, 2: a, keep the old API`). "
              "The next `agent-kit run` (or `run --wait` poll) continues the same session with your answer.",
              "", marker(n, rnd)]
    return "\n".join(parts)


def answers(comments, n, rnd, trusted, kit_marker):
    """Bodies of the comments that answer round rnd: posted after the questions comment, by a trusted author
    association, not the kit's own, not a bot, not empty. [] while unanswered (or the questions comment is gone)."""
    mk = marker(n, rnd)
    at = max((i for i, x in enumerate(comments) if mk in (x.get("body") or "")), default=None)
    if at is None:
        return []
    return [x["body"].strip() for x in comments[at + 1:]
            if x.get("association") in trusted and (x.get("body") or "").strip()
            and kit_marker not in x["body"] and "<!-- agent-kit questions" not in x["body"]
            and not (x.get("author") or "").endswith("[bot]")]


def qa(items, replies):
    """The questions and the human's answer, for the resume prompt."""
    qs = "\n".join(f"{i}. {q['question'].strip()} Options: "
                   + "; ".join(f"{chr(97 + j)}) {o.strip()}" for j, o in enumerate(q.get("options") or []))
                   for i, q in enumerate(items, 1))
    return f"## Your questions\n{qs}\n\n## The human's answer\n" + "\n\n".join(replies)
