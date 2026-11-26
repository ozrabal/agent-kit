"""After the PR is open: decide which review comments and CI failures need work. Pure functions;
the loop (loop.py) calls the tracker, the LLM and git around them."""

MARKER = "<!-- agent-kit -->"  # on every comment the kit posts, so it never triages its own text


def new_comments(comments, handled, trusted):
    """Comments that still need triage: by a trusted author association, not ours (marker), not a bot,
    thread not resolved, not empty, and id not in handled. Issue comments and review bodies have no
    thread, so they count until their id is handled."""
    done = set(handled or [])
    return [x for x in comments
            if x["id"] not in done and x.get("association") in trusted and not x.get("resolved")
            and (x.get("body") or "").strip() and MARKER not in x["body"]
            and not (x.get("author") or "").endswith("[bot]")]


def ci_decision(checks, sha, handled_shas, rounds, max_rounds):
    """What to do about CI on head sha: {"action": none|pending|handled|limit|fix, "reason", "failed"}.
    Acts only when no check is pending, so one fix sees every failed log."""
    failed = [x for x in checks if x.get("bucket") == "fail"]
    if any(x.get("bucket") == "pending" for x in checks):
        return {"action": "pending", "reason": f"CI pending on {sha[:7]}", "failed": failed}
    if not failed:
        return {"action": "none", "reason": f"CI {'green' if checks else 'has no checks'} on {sha[:7]}", "failed": []}
    names = ", ".join(x["name"] for x in failed)
    if sha in (handled_shas or []):
        return {"action": "handled", "reason": f"CI failed on {sha[:7]} ({names}), already handled", "failed": failed}
    if rounds >= max_rounds:
        return {"action": "limit", "reason": f"CI failed on {sha[:7]} ({names}); CI rounds used: {rounds} of {max_rounds}",
                "failed": failed}
    return {"action": "fix", "reason": f"CI failed on {sha[:7]} ({names})", "failed": failed}


def merge_decision(pr, ci_action, checks, require_checks):
    """merge.policy "auto": {"action": merge|wait|conflict, "reason"} for an open PR on a poll where nothing
    else happened. pr is {isDraft, mergeable, reviewDecision}; ci_action from ci_decision()."""
    if pr.get("isDraft"):
        return {"action": "wait", "reason": "draft PR"}
    if ci_action != "none":
        return {"action": "wait", "reason": f"CI {ci_action}"}
    if not checks and require_checks:
        return {"action": "wait", "reason": "no CI checks yet (merge.require_checks)"}
    if pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        return {"action": "wait", "reason": f"review: {pr['reviewDecision'].lower().replace('_', ' ')}"}
    if pr.get("mergeable") == "CONFLICTING":
        return {"action": "conflict", "reason": "merge conflict with the base branch"}
    if pr.get("mergeable") != "MERGEABLE":
        return {"action": "wait", "reason": f"mergeable: {(pr.get('mergeable') or 'unknown').lower()}"}
    return {"action": "merge", "reason": "CI green, nothing new"}


def where(x):
    """"todo/store.py:7" for an inline comment, "PR review" or "PR conversation" for the others."""
    if x["kind"] == "inline":
        return f"{x['path']}:{x['line']}" if x.get("line") else x["path"]
    return "PR review" if x["kind"] == "review" else "PR conversation"


def check_lines(checks):
    return "\n".join(f"- {x['name']}: {x.get('link') or ''}" for x in checks)


def _head(x):
    return f"({where(x)}, by {x.get('author') or '?'})"


def quote(body, n=300):
    return "\n".join("> " + l for l in body.strip()[:n].splitlines())


def render(x):
    """One comment for the triage and fix prompts: id, place, author, text, and the diff hunk it sits on."""
    head = f"### id {x['id']} {_head(x)}\n{x['body'].strip()}"
    return head + (f"\n```diff\n{x['diff_hunk']}\n```" if x.get("diff_hunk") else "")


def reply_md(x, draft):
    return f"## comment {x['id']} {_head(x)}\n\n{quote(x['body'])}\n\n{draft.strip()}\n\n"


def poll_text(p):
    return f"error: {p['error']}" if p.get("error") else f"{p['comments']} new comment(s), {p['note']}"


def status_note(state, limits):
    """`agent-kit status` text for a run's PR follow-up, from state.json only (no gh call). "" before the first poll."""
    polls = state.get("polls") or []
    if not polls:
        return ""
    parts = [f"polls={state.get('poll_count') or len(polls)} last: {poll_text(polls[-1])}"]
    handled = set(state.get("handled_comments") or [])
    pending = [t["id"] for t in state.get("triage") or [] if t["action"] == "fix" and t["id"] not in handled]
    if pending:
        parts.append(f"{len(pending)} fix comment(s) pending")
    parts.append(f"rounds ci {state.get('ci_rounds') or 0}/{limits['max_ci_rounds']} "
                 f"comments {state.get('comment_rounds') or 0}/{limits['max_comment_rounds']}")
    if state.get("status") == "needs_human" and state.get("needs_human"):
        parts.append(f"needs a human: {state['needs_human']['reason']}")
    return "; ".join(parts)
