"""Project knowledge base in <repo>/.knowledge/ (committed, except the sqlite index).

files.jsonl     one row per source file: hash, symbols, imports, one-line summary
tree.md         human/agent-readable code map built from files.jsonl
tickets/*.md    what each finished ticket changed and learned
gotchas.md      pitfalls (from failed gates and review findings)
conventions.md  project rules reviews enforce
index.sqlite    FTS5 search index, rebuilt from the files above (gitignored)

Everything is parsed by scripts. The LLM only writes summaries for files that have
no header comment, and only again when a file's symbols or size change a lot.
"""
import difflib
import fnmatch
import hashlib
import json
import posixpath
import re
import sqlite3
from pathlib import Path

from . import llm
from .util import clip, log, out

LANG = {
    ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".vue": "js", ".svelte": "js", ".py": "py", ".rs": "rs", ".go": "go", ".java": "oo",
    ".kt": "oo", ".swift": "oo", ".rb": "oo", ".php": "oo", ".cs": "oo", ".c": "c", ".h": "c",
    ".cpp": "c", ".hpp": "c", ".sql": "sql", ".sh": "sh", ".md": "md", ".toml": "cfg",
    ".yml": "cfg", ".yaml": "cfg", ".css": "css", ".scss": "css",
}

SYM = {
    "js": [r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
           r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
           r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"],
    "py": [r"^(?:async\s+)?def\s+([A-Za-z_]\w*)", r"^class\s+([A-Za-z_]\w*)"],
    "rs": [r"^\s*pub(?:\([\w:]+\))?\s+(?:(?:async|const|unsafe|extern)\s+)*"
           r"(?:fn|struct|enum|trait|type|const|static|mod)\s+([A-Za-z_]\w*)",
           r"^(?:async\s+)?fn\s+([A-Za-z_]\w*)", r"^(?:struct|enum|trait)\s+([A-Za-z_]\w*)"],
    "go": [r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", r"^type\s+([A-Za-z_]\w*)"],
    "oo": [r"^\s*(?:(?:public|private|protected|internal|export|open|abstract|final|static|"
           r"sealed|data)\s+)*(?:class|interface|enum|struct|object|record|trait|protocol|fun|func|"
           r"def|function)\s+([A-Za-z_]\w*)"],
    "c": [r"^[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;]*$", r"^(?:typedef\s+)?struct\s+([A-Za-z_]\w*)"],
    "sql": [r"(?i)^\s*create\s+(?:unique\s+)?(?:table|index|view|trigger)\s+(?:if\s+not\s+exists\s+)?([\w.]+)"],
    "sh": [r"^(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{"],
    "md": [r"^#{1,3}\s+(.+?)\s*#*$"],
}
SYM = {k: [re.compile(p, re.M) for p in v] for k, v in SYM.items()}

JS_IMPORT = re.compile(
    r"""(?:import|export)\s[^'";]*?from\s*['"]([^'"]+)['"]|import\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|require\(\s*['"]([^'"]+)['"]\s*\)|^\s*import\s+['"]([^'"]+)['"]""", re.M)
PY_IMPORT = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import|^\s*import\s+([\w.]+)", re.M)
RS_MOD = re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?mod\s+(\w+)\s*;", re.M)
RS_CRATE = re.compile(r"\bcrate::((?:\w+::)*\w+)")
JS_EXT = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".d.ts"]

STOP = set("""a an and are as at be been but by can could do does each for from has have how
if in into is it its may more must no not of on once only or other our out over same should
so some such than that the their then there these they this those to under up use used uses
using via was we were what when where which while who will with within without would you your
also any all both every new add adds added make makes just like need needs none none
ticket issue build what's acceptance criteria parent blocked""".split())

SUM_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["path", "summary"]}}},
    "required": ["items"],
}
SUM_SYSTEM = ("You index a codebase. For each file write ONE line, at most 18 words, saying what "
              "the file is responsible for, in domain terms. Never start with 'This file'. "
              "No implementation trivia.")

SEEDS = {
    "gotchas.md": "# Gotchas\n\nPitfalls learned from past tickets (failed gates, review findings). "
                  "One bullet each. Edit freely.\n\n",
    "conventions.md": "# Conventions\n\nProject rules that reviews enforce and agents read before "
                      "every task. Keep it short. Edit freely.\n\n",
}


def kdir(cfg, cwd):
    return Path(cwd) / cfg["kb"]["dir"]


def ensure_seeds(cfg, cwd):
    d = kdir(cfg, cwd)
    (d / "tickets").mkdir(parents=True, exist_ok=True)
    for name, text in SEEDS.items():
        if not (d / name).exists():
            (d / name).write_text(text)


# --- parsing ---------------------------------------------------------------

def list_files(cfg, cwd):
    k = cfg["kb"]
    files = out(["git", "ls-files", "-co", "--exclude-standard"], cwd=cwd).splitlines()
    res = []
    for p in files:
        ext = Path(p).suffix.lower()
        if ext not in k["include_ext"] or any(fnmatch.fnmatch(p, g) for g in k["exclude"]):
            continue
        f = Path(cwd) / p
        if f.is_file() and f.stat().st_size <= k["max_file_kb"] * 1024:
            res.append(p)
    return sorted(set(res))


def symbols(text, lang):
    seen, res = set(), []
    for rx in SYM.get(lang, []):
        for m in rx.finditer(text):
            s = m.group(1).strip()
            if s not in seen and len(s) < 80:
                seen.add(s)
                res.append(s)
    return res[: 15 if lang == "md" else 40]


def _first_sentence(s, n=200):
    s = re.sub(r"\s+", " ", s or "").strip()
    m = re.search(r"(?<=[.!?])\s", s)
    s = s[: m.start()] if m and m.start() > 20 else s
    return s[:n]


_JUNK = re.compile(r"copyright|licen[sc]e|eslint|prettier|@ts-|tslint|auto-?generated|do not edit|"
                   r"^\s*@\w+|spdx", re.I)


def doc_summary(text, lang):
    """A summary from the file's own header comment or first paragraph, if any."""
    lines = text.splitlines()
    if lang == "md":
        title = para = None
        for l in lines:
            s = l.strip()
            if not title and s.startswith("#"):
                title = s.lstrip("#").strip()
                continue
            if title and re.match(r"[A-Za-z*_`\"'(]", s) and not re.match(r"\d+\.\s|\*\s|\[|\*\*[^*]{1,40}\*\*\s*:|\*\*[^*]{1,40}:\*\*", s) \
                    and len(re.findall(r"[A-Za-z]", s)) > 20:
                para = s
                break
        if title:
            return clip(f"{title}: {_first_sentence(para)}" if para else title, 200).strip()
        return None
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#!")
                              or re.match(r"""^\s*['"]use \w+['"];?\s*$""", lines[i])):
        i += 1
    if i >= len(lines):
        return None
    s = lines[i].strip()
    block = []
    if lang == "py" and s[:3] in ('"""', "'''"):
        q = s[:3]
        body = s[3:]
        if q in body:
            block = [body.split(q)[0]]
        else:
            block = [body]
            for l in lines[i + 1:]:
                if q in l:
                    block.append(l.split(q)[0])
                    break
                block.append(l)
    elif s.startswith("/*"):
        for l in lines[i:]:
            block.append(re.sub(r"^\s*/?\*+/?|\*/\s*$", "", l))
            if "*/" in l:
                break
    else:
        marks = ("//", "#", "--") if lang in ("sh", "py", "cfg", "sql") else ("//",)
        for l in lines[i:]:
            ls = l.strip()
            if not ls.startswith(marks) or ls.startswith("#["):
                break
            block.append(re.sub(r"^(//[/!]?|#|--)\s?", "", ls))
    text = " ".join(b.strip() for b in block if b.strip())
    if len(text) < 12 or _JUNK.search(text[:120]):
        return None
    return _first_sentence(text)


def _strip_jsonc(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)(^|[^:\"'])//.*$", r"\1", s)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def aliases(cwd):
    res = []
    for name in ("tsconfig.json", "jsconfig.json"):
        f = Path(cwd) / name
        if not f.exists():
            continue
        try:
            co = json.loads(_strip_jsonc(f.read_text())).get("compilerOptions", {})
        except json.JSONDecodeError:
            continue
        base = co.get("baseUrl", ".")
        for k, targets in (co.get("paths") or {}).items():
            if targets:
                pre = k.rstrip("*")
                tgt = posixpath.normpath(posixpath.join(base, targets[0].rstrip("*")))
                res.append((pre, tgt + ("/" if targets[0].endswith("/*") else "")))
    return res


def _resolve_js(spec, path, fileset, als):
    if spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    else:
        for pre, tgt in als:
            if pre and spec.startswith(pre):
                base = posixpath.normpath(tgt + spec[len(pre):])
                break
        else:
            return None
    cands = [base] + [base + e for e in JS_EXT] + [base + "/index" + e for e in JS_EXT]
    if base.endswith(".js"):
        cands += [base[:-3] + e for e in (".ts", ".tsx")]
    return next((c for c in cands if c in fileset), None)


def _resolve_py(spec, path, fileset):
    dots = len(spec) - len(spec.lstrip("."))
    mod = spec.lstrip(".").replace(".", "/")
    if dots:
        base = posixpath.dirname(path)
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        roots = [base + "/" if base else ""]
    else:
        roots = ["", "src/"]
    for r in roots:
        for c in (f"{r}{mod}.py", f"{r}{mod}/__init__.py"):
            if c in fileset:
                return c
    return None


def _crate_src(path, cwd):
    d = posixpath.dirname(path)
    while True:
        if (Path(cwd) / d / "Cargo.toml").exists():
            return posixpath.join(d, "src") if d else "src"
        if not d:
            return None
        d = posixpath.dirname(d)


def imports(text, lang, path, fileset, als, cwd):
    found = set()
    if lang == "js":
        for m in JS_IMPORT.finditer(text):
            spec = next(g for g in m.groups() if g)
            r = _resolve_js(spec, path, fileset, als)
            if r:
                found.add(r)
    elif lang == "py":
        for m in PY_IMPORT.finditer(text):
            r = _resolve_py(m.group(1) or m.group(2), path, fileset)
            if r:
                found.add(r)
    elif lang == "rs":
        d, stem = posixpath.dirname(path), Path(path).stem
        base = d if stem in ("mod", "lib", "main") else posixpath.join(d, stem)
        for m in RS_MOD.finditer(text):
            for c in (f"{base}/{m.group(1)}.rs", f"{base}/{m.group(1)}/mod.rs"):
                if c in fileset:
                    found.add(c)
        src = _crate_src(path, cwd)
        if src:
            for m in RS_CRATE.finditer(text):
                parts = m.group(1).split("::")
                for n in range(len(parts), 0, -1):
                    p = "/".join(parts[:n])
                    hit = next((c for c in (f"{src}/{p}.rs", f"{src}/{p}/mod.rs") if c in fileset), None)
                    if hit:
                        found.add(hit)
                        break
    found.discard(path)
    return sorted(found)


# --- build / update --------------------------------------------------------

def _load_rows(d):
    f = d / "files.jsonl"
    if not f.exists():
        return {}
    return {r["path"]: r for r in (json.loads(l) for l in f.read_text().splitlines() if l.strip())}


def _similar(old, new):
    return (set(old.get("symbols", [])) == set(new["symbols"])
            and abs(old.get("lines", 0) - new["lines"]) <= max(10, 0.3 * old.get("lines", 0)))


def _llm_summaries(cfg, cwd, todo, cost_log):
    k, done, batch, size = cfg["kb"], {}, [], 0

    def flush():
        nonlocal batch, size
        if not batch:
            return
        prompt = "Summarize each file below. Return one item per file, same paths.\n\n" + "\n\n".join(batch)
        try:
            res, _ = llm.claude(prompt, cwd=cwd, step="summarize", model=cfg["models"]["summarize"],
                                effort=cfg["effort"]["summarize"], schema=SUM_SCHEMA, lean=True,
                                system=SUM_SYSTEM, tools="", budget=cfg["budget_usd"]["summarize"],
                                cost_log=cost_log)
            for it in res.get("items", []):
                done[it["path"]] = it["summary"].strip()
        except llm.LLMError as e:
            log(f"summary batch failed, continuing: {e}")
        batch, size = [], 0

    for row, text in todo:
        head = "\n".join(l[:160] for l in text.splitlines()[:60])
        chunk = f"### {row['path']}\nsymbols: {', '.join(row['symbols'][:15]) or '-'}\n```\n{head}\n```"
        if size + len(chunk) > k["batch_chars"]:
            flush()
        batch.append(chunk)
        size += len(chunk)
    flush()
    return done


def update(cfg, cwd, full=False, use_llm=True, cost_log=None):
    d = kdir(cfg, cwd)
    ensure_seeds(cfg, cwd)
    old = {} if full else _load_rows(d)
    files = list_files(cfg, cwd)
    fileset, als = set(files), aliases(cwd)
    rows, todo, changed = [], [], 0
    for p in files:
        raw = (Path(cwd) / p).read_bytes()
        h = hashlib.sha1(raw).hexdigest()[:12]
        o = old.get(p)
        if o and o.get("hash") == h:
            rows.append(o)
            if o.get("src") == "none" and use_llm:  # summary skipped earlier: fill it now
                todo.append((o, raw.decode("utf-8", errors="replace")))
            continue
        changed += 1
        text = raw.decode("utf-8", errors="replace")
        lang = LANG.get(Path(p).suffix.lower(), "other")
        row = {"path": p, "hash": h, "lang": lang, "lines": text.count("\n") + 1,
               "symbols": symbols(text, lang), "imports": imports(text, lang, p, fileset, als, cwd),
               "summary": "", "src": "none"}
        ds = doc_summary(text, lang)
        if ds:
            row.update(summary=ds, src="doc")
        elif o and o.get("summary") and _similar(o, row):
            row.update(summary=o["summary"], src=o.get("src", "llm"))
        else:
            todo.append((row, text))
        rows.append(row)
    n_llm = 0
    if todo and use_llm and cfg["kb"]["llm_summaries"]:
        log(f"kb: summarizing {len(todo)} files without a header comment")
        got = _llm_summaries(cfg, cwd, todo, cost_log)
        for row, _ in todo:
            if row["path"] in got:
                row.update(summary=got[row["path"]], src="llm")
                n_llm += 1
    rows.sort(key=lambda r: r["path"])
    (d / "files.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    _write_tree(d, rows)
    removed = len(set(old) - fileset)
    log(f"kb: {len(rows)} files, {changed} changed, {removed} removed, {n_llm} LLM summaries")
    return {"files": len(rows), "changed": changed, "removed": removed, "llm": n_llm}


def _write_tree(d, rows):
    lines = ["# Code map", "",
             "Generated by `agent-kit kb update`. One line per file: what it is responsible for.",
             "Search it with `agent-kit kb context \"<words>\"`.", ""]
    cur = None
    for r in sorted(rows, key=lambda r: (posixpath.dirname(r["path"]), r["path"])):
        dirn = posixpath.dirname(r["path"]) or "."
        if dirn != cur:
            lines += ["", f"## {dirn}"]
            cur = dirn
        s = r["summary"] or (", ".join(r["symbols"][:6]) if r["symbols"] else "")
        lines.append(f"- `{posixpath.basename(r['path'])}`" + (f" — {s}" if s else ""))
    (d / "tree.md").write_text("\n".join(lines) + "\n")


# --- index and retrieval ---------------------------------------------------

def _expand(text):
    toks = re.findall(r"[A-Za-z][A-Za-z0-9]*", text or "")
    parts = []
    for t in toks:
        parts.append(t.lower())
        sub = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", t)
        if len(sub) > 1:
            parts += [s.lower() for s in sub]
    return " ".join(parts)


def _sources(d):
    return [d / "files.jsonl", d / "gotchas.md", d / "conventions.md", *sorted((d / "tickets").glob("*.md"))]


def index(cfg, cwd):
    d = kdir(cfg, cwd)
    dbp = d / "index.sqlite"
    srcs = [p for p in _sources(d) if p.exists()]
    if not (d / "files.jsonl").exists():
        raise SystemExit("no knowledge base yet: run `agent-kit kb update` first")
    if dbp.exists() and dbp.stat().st_mtime >= max(p.stat().st_mtime for p in srcs):
        return sqlite3.connect(dbp)
    dbp.unlink(missing_ok=True)
    db = sqlite3.connect(dbp)
    db.executescript("""
        CREATE VIRTUAL TABLE files USING fts5(path, symbols, summary, raw UNINDEXED);
        CREATE TABLE edges(src TEXT, dst TEXT);
        CREATE VIRTUAL TABLE tickets USING fts5(key, title, body);
        CREATE VIRTUAL TABLE notes USING fts5(kind UNINDEXED, text);
    """)
    for r in _load_rows(d).values():
        db.execute("INSERT INTO files VALUES (?,?,?,?)",
                   (_expand(r["path"].replace("/", " ")), _expand(" ".join(r["symbols"])),
                    _expand(r["summary"]), json.dumps(r)))
        db.executemany("INSERT INTO edges VALUES (?,?)", [(r["path"], x) for x in r["imports"]])
    for t in sorted((d / "tickets").glob("*.md")):
        body = t.read_text()
        title = body.splitlines()[0].lstrip("# ") if body else t.stem
        db.execute("INSERT INTO tickets VALUES (?,?,?)", (t.stem, title, _expand(body) + "\n" + body))
    for kind in ("gotchas", "conventions"):
        f = d / f"{kind}.md"
        if f.exists():
            for l in f.read_text().splitlines():
                if l.startswith("- "):
                    db.execute("INSERT INTO notes VALUES (?,?)", (kind, l[2:]))
    db.commit()
    return db


def terms(text, limit=30):
    text = text or ""
    ticks = re.findall(r"`([^`]{2,80})`", text)
    freq = {}
    for i, chunk in enumerate([" ".join(ticks), text]):
        for t in _expand(chunk).split():
            if len(t) >= 3 and t not in STOP and not t.isdigit():
                freq[t] = freq.get(t, 0) + (3 if i == 0 else 1)
    return [t for t, _ in sorted(freq.items(), key=lambda x: -x[1])][:limit]


def _fts(q_terms):
    return " OR ".join('"' + t.replace('"', "") + '"' for t in q_terms)


def search_files(db, text, n):
    q = terms(text)
    if not q:
        return []
    rows = db.execute("SELECT raw FROM files WHERE files MATCH ? ORDER BY bm25(files, 2.0, 3.0, 1.0) LIMIT ?",
                      (_fts(q), n)).fetchall()
    return [json.loads(r[0]) for r in rows]


def context(cfg, cwd, text, key=None, related_keys=()):
    """A size-capped markdown pack of what an agent should read first for a task."""
    k = cfg["kb"]
    d = kdir(cfg, cwd)
    db = index(cfg, cwd)
    allrows = {json.loads(r[0])["path"]: json.loads(r[0]) for r in db.execute("SELECT raw FROM files")}
    picked = []
    for p in allrows:  # files named explicitly in the task come first
        if p in text or (len(posixpath.basename(p)) > 6 and f"`{posixpath.basename(p)}`" in text):
            picked.append(allrows[p])
    for r in search_files(db, text, k["context_files"] * 2):
        if r["path"] not in {x["path"] for x in picked}:
            picked.append(r)
    picked = picked[: k["context_files"]]
    have = {r["path"] for r in picked}

    def line(r, extra=""):
        sym = ", ".join(r["symbols"][:8])
        return f"- `{r['path']}` — {r['summary'] or '(no summary)'}" + (f" [{sym}]" if sym else "") + extra

    out_ = [f"# Context pack{f' for {key}' if key else ''}",
            "Built by scripts from .knowledge/. A starting point, not the full truth: verify in code.", "",
            "## Likely relevant files", *[line(r) for r in picked]]

    neigh = []
    for r in picked[:5]:
        for (dst,) in db.execute("SELECT dst FROM edges WHERE src=?", (r["path"],)):
            if dst not in have and dst in allrows:
                neigh.append((dst, f" (imported by {posixpath.basename(r['path'])})"))
                have.add(dst)
        for (src,) in db.execute("SELECT src FROM edges WHERE dst=?", (r["path"],)):
            if src not in have and src in allrows:
                neigh.append((src, f" (imports {posixpath.basename(r['path'])})"))
                have.add(src)
    if neigh:
        out_ += ["", "## Neighbours in the import graph", *[line(allrows[p], why) for p, why in neigh[:10]]]

    tk = []
    for rk in related_keys:
        f = d / "tickets" / f"{rk}.md"
        if f.exists():
            tk.append(f"### {rk} (blocker)\n" + clip(f.read_text(), 1500))
    q = terms(text)
    if q:
        for kk, body in db.execute("SELECT key, body FROM tickets WHERE tickets MATCH ? ORDER BY rank LIMIT 4",
                                   (_fts(q),)):
            if kk != key and kk not in related_keys and len(tk) < 5:
                raw = body.split("\n", 1)[1] if "\n" in body else body
                tk.append(f"### {kk}\n" + clip(raw, 600))
    if tk:
        out_ += ["", "## Related past tickets", *tk]

    g = d / "gotchas.md"
    if g.exists():
        bullets = [l for l in g.read_text().splitlines() if l.startswith("- ")]
        if len("\n".join(bullets)) > 2500 and q:
            bullets = ["- " + t for (t,) in db.execute(
                "SELECT text FROM notes WHERE kind='gotchas' AND notes MATCH ? ORDER BY rank LIMIT 15", (_fts(q),))]
        if bullets:
            out_ += ["", "## Gotchas from past tickets", *bullets]
    c = d / "conventions.md"
    if c.exists():
        bullets = [l for l in c.read_text().splitlines() if l.startswith("- ")]
        if bullets:
            out_ += ["", "## Conventions", clip("\n".join(bullets), 3000)]
    return clip("\n".join(out_), k["context_chars"])


# --- learning --------------------------------------------------------------

def write_ticket(cfg, cwd, key, title, number, summary, files_stat, lessons, pr=None):
    d = kdir(cfg, cwd)
    ensure_seeds(cfg, cwd)
    body = [f"# {key}: {title}", "", f"Issue #{number}" + (f" · PR {pr}" if pr else ""), "",
            "## What changed", summary.strip(), "", "## Files", files_stat.strip() or "-"]
    if lessons:
        body += ["", "## Lessons", *[f"- {l}" for l in lessons]]
    safe = re.sub(r"[^\w.-]", "_", key)
    (d / "tickets" / f"{safe}.md").write_text("\n".join(body) + "\n")


def add_lessons(cfg, cwd, items):
    """items: [(kind, text, key)], kind in gotchas|conventions. Skips near-duplicates."""
    d = kdir(cfg, cwd)
    ensure_seeds(cfg, cwd)
    added = 0
    for kind, text, key in items:
        f = d / f"{'conventions' if kind.startswith('conv') else 'gotchas'}.md"
        cur = f.read_text()
        existing = [l[2:].lower() for l in cur.splitlines() if l.startswith("- ")]
        t = re.sub(r"\s+", " ", text).strip().rstrip(".")
        if not t or any(difflib.SequenceMatcher(None, t.lower(), e).ratio() > 0.8 for e in existing):
            continue
        f.write_text(cur.rstrip("\n") + f"\n- {t} ({key})\n")
        added += 1
    return added


def stats(cfg, cwd):
    d = kdir(cfg, cwd)
    rows = _load_rows(d)
    src = {}
    for r in rows.values():
        src[r.get("src", "none")] = src.get(r.get("src", "none"), 0) + 1
    count = lambda f: sum(1 for l in (d / f).read_text().splitlines() if l.startswith("- ")) if (d / f).exists() else 0
    return {"files": len(rows), "summaries": src, "edges": sum(len(r["imports"]) for r in rows.values()),
            "tickets": len(list((d / "tickets").glob("*.md"))) if (d / "tickets").exists() else 0,
            "gotchas": count("gotchas.md"), "conventions": count("conventions.md")}
