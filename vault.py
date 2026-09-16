"""The Obsidian bridge: resolve a dictation's referents from the speaker's own vault, locally.

Promptify's engine gets short excerpts from matching notes as <vault_context>, so "our app" or
"second-brain" need not become a clarifying question. Everything here is stdlib and local; the
excerpts leave the machine only inside the one engine call the user triggers.

The design is forced by the measured mount: the user's vault lives on a read-only rclone mount
of Google Drive where a cold directory walk took 198 s and a full content scan never finished
(killed after 18 min). So the hot path touches ONLY a cached index (%APPDATA%\\murmur\\
vault_index.json: per note, the path, title, lookup names, tags, a <=300-char description from
the head of the file) and any live read is deadline-guarded; the index is built and refreshed on
a daemon thread, never while a draft waits. Matching inverts the usual direction: the dictation
is spoken, lowercase text, so the index's NAME dictionary is matched against the dictation's
token stream ("second brain" finds second-brain.md) instead of hunting for proper nouns.

Private folders (journal, diary, private, people) are excluded at index time by default, along
with .obsidian, templates and anything the user excludes; changing the vault or the exclusions
deletes the index before rebuilding, so nothing excluded lingers from an older build.
"""
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

SKIP_DIRS = {".obsidian", ".trash", ".git", "$recycle.bin", "node_modules", "__pycache__"}
PRIVATE_DIRS = {"journal", "diary", "private", "people"}
HEAD_BYTES = 4096            # what indexing reads per file: frontmatter + H1 + first paragraph
MAX_INDEX_NOTES = 20000      # a huge vault keeps its newest notes
INDEX_TTL_S = 6 * 3600
MAX_NOTES = 3
NOTE_BUDGET = 1200           # chars per injected note
TOTAL_BUDGET = 3000
STOP = set("""the and for with that this from into your our are was were will would could should have has had
about just like kind you know what when where which while been being over under after before then than them
they there here very much many more most some any all not can may might app apps code test tests note notes
file files new old make made set use used using get got run runs need want going right okay also really
thing things stuff way ways one two how why who let say said see look""".split())

log = print                  # murmur.py points this at its own log()
_building = {"flag": threading.Event(), "started": 0.0}


def index_path(appdir) -> Path:
    return Path(appdir) / "vault_index.json"


def known_vaults() -> list:
    """The vaults Obsidian itself knows on this machine, existing paths first."""
    try:
        j = json.loads((Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json")
                       .read_text(encoding="utf-8"))
        paths = [v.get("path") for v in (j.get("vaults") or {}).values() if v.get("path")]
        return sorted(set(paths), key=lambda p: not Path(p).exists())
    except (OSError, ValueError, AttributeError):
        return []


def _norm_name(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)          # camelCase -> spaced
    return re.sub(r"[-_]+", " ", s).strip().lower()


def _frontmatter(head: str) -> dict:
    """The few YAML shapes this needs: `key: value` and `- item` lists. A parse failure just
    means fewer fields - no YAML lib exists in the stdlib and vault frontmatter is simple."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", head, re.S)
    out = {}
    if not m:
        return out
    key = None
    for line in m.group(1).splitlines():
        mk = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if mk:
            key = mk.group(1).lower()
            v = mk.group(2).strip().strip("'\"")
            out[key] = v if v else []
        elif key is not None and re.match(r"^\s*-\s+", line):
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(line.split("-", 1)[1].strip().strip("'\""))
    return out


def _entry(vault: Path, p: Path, head: str, st) -> dict:
    rel = p.relative_to(vault).as_posix()
    fm = _frontmatter(head)
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", head, flags=re.S)
    h1 = re.search(r"^#\s+(.+)$", body, re.M)
    title = (h1.group(1).strip() if h1 else "") or str(fm.get("name") or "") or p.stem
    names = {p.stem}
    if p.name.lower() in ("readme.md", "index.md"):
        names.add(p.parent.name)                 # a project hub's real name is its folder
        title = p.parent.name if not h1 else title
    if h1:
        names.add(h1.group(1).strip())
    if isinstance(fm.get("name"), str):
        names.add(fm["name"])
    for a in fm.get("aliases") or []:
        names.add(a)
    para = next((ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.startswith("#")), "")
    desc = (str(fm.get("description") or "") or para)[:300]
    tags = [str(t).lstrip("#").lower() for t in (fm.get("tags") or []) if str(t).strip()]
    # the snippet material is cached at index time so the hot path never touches the mount
    # (a live read through rclone cost up to the whole 2 s deadline per draft)
    lines = [f"{k}: {fm[k]}" for k in ("description", "status", "next")
             if isinstance(fm.get(k), str) and fm[k]]
    return {"p": rel, "title": title, "names": sorted({_norm_name(n) for n in names if n}),
            "tags": tags, "type": str(fm.get("type") or ""), "desc": desc,
            "fm": "\n".join(lines), "head": body[:1800],
            "mtime": int(st.st_mtime), "size": st.st_size}


def build_index(vault_path: str, appdir, excludes=(), cancel=None, old=None) -> dict:
    """Walk the vault, head-read every .md, write the index atomically. Runs on a daemon
    thread only - a cold build through the rclone mount costs minutes. Entries whose
    (mtime, size) are unchanged reuse the old parse instead of re-reading."""
    vault = Path(vault_path)
    prev = {e["p"]: e for e in (old or {}).get("notes", [])}
    skip_prefixes = [x.strip().lower().replace("\\", "/").rstrip("/") + "/" for x in excludes if x.strip()]
    notes = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs
                   if d.lower() not in SKIP_DIRS and d.lower() not in PRIVATE_DIRS
                   and not d.startswith(".")]
        rel_root = Path(root).relative_to(vault).as_posix().lower()
        if any((rel_root + "/").startswith(x) for x in skip_prefixes):
            dirs[:] = []
            continue
        for f in files:
            if cancel is not None and cancel.is_set():
                return {}
            if not f.lower().endswith(".md"):
                continue
            p = Path(root) / f
            try:
                st = p.stat()
                rel = p.relative_to(vault).as_posix()
                was = prev.get(rel)
                if was and was["mtime"] == int(st.st_mtime) and was["size"] == st.st_size:
                    notes.append(was)
                    continue
                head = p.read_bytes()[:HEAD_BYTES].decode("utf-8", "replace")
                notes.append(_entry(vault, p, head, st))
            except OSError:
                continue
    notes.sort(key=lambda e: -e["mtime"])
    idx = {"v": 2, "vault": str(vault), "built": time.time(), "excludes": sorted(skip_prefixes),
           "notes": notes[:MAX_INDEX_NOTES]}
    ip = index_path(appdir)
    ip.parent.mkdir(parents=True, exist_ok=True)
    tmp = ip.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ip)
    log(f"  vault: indexed {len(idx['notes'])} notes")
    return idx


def load_index(appdir):
    try:
        j = json.loads(index_path(appdir).read_text(encoding="utf-8"))
        # v2 carries the snippet material; an older index rebuilds rather than degrade
        return j if isinstance(j, dict) and isinstance(j.get("notes"), list) and j.get("v") == 2 else None
    except (OSError, ValueError):
        return None


def refresh_if_stale(cfg: dict, appdir) -> None:
    """Kick a background rebuild when the index is missing, old, or built for another vault or
    exclusion list (that case deletes the old index first, so nothing excluded lingers).
    Single-flight; a build on a hung mount never completes, so a new attempt is allowed after
    30 minutes. Never blocks the caller."""
    vault = (cfg.get("vault_path") or "").strip()
    if not vault:
        return
    excl = sorted(x.strip().lower().replace("\\", "/").rstrip("/") + "/"
                  for x in (cfg.get("vault_exclude") or []) if x.strip())
    idx = load_index(appdir)
    mismatched = idx is not None and (idx.get("vault") != vault or idx.get("excludes") != excl)
    fresh = idx is not None and not mismatched and time.time() - idx.get("built", 0) < INDEX_TTL_S
    if fresh:
        return
    if _building["flag"].is_set() and time.time() - _building["started"] < 1800:
        return
    if mismatched:
        try:
            index_path(appdir).unlink()
        except OSError:
            pass
        idx = None
    _building["flag"].set()
    _building["started"] = time.time()

    def work():
        try:
            build_index(vault, appdir, cfg.get("vault_exclude") or (), old=idx)
        except Exception as e:                      # a vault bug must never surface anywhere
            log(f"  vault: index failed ({e})")
        finally:
            _building["flag"].clear()
    threading.Thread(target=work, daemon=True).start()


# --- the hot path: match, score, snippet ----------------------------------------------------
def extract_terms(dictation: str, vocab="") -> list:
    """Content-side terms for desc/tag scoring: quoted spans, hyphen/camel tokens, capitalized
    runs, and any vocab.txt term present - the user's own proper-noun list."""
    out = []
    out += re.findall(r'"([^"]{3,40})"', dictation)
    out += re.findall(r"\b\w+(?:-\w+)+\b", dictation)
    out += re.findall(r"\b[a-z]+[A-Z]\w+\b", dictation)
    out += re.findall(r"\b[A-Z][a-z]+(?:[A-Z]\w*)+\b", dictation)      # RowList-style camel too
    out += [" ".join(t) for t in re.findall(r"\b([A-Z]\w+)[ ]([A-Z]\w+)\b", dictation)]
    low = dictation.lower()
    for v in re.split(r"[,\n]", vocab or ""):
        v = v.strip().rstrip(".")
        if len(v) >= 3 and v.lower() in low:
            out.append(v)
    seen, terms = set(), []
    for t in out:
        n = _norm_name(t)
        if n and n not in seen and n not in STOP:
            seen.add(n)
            terms.append(n)
    return terms[:8]


def search(index: dict, dictation: str, vocab="") -> list:
    """Score every indexed note against the dictation, no IO. A note needs at least one real
    name hit (score 100) to qualify - vault context is a bonus, never padding."""
    toks = re.findall(r"\w+", dictation.lower())
    joined = " " + " ".join(toks) + " "
    terms = extract_terms(dictation, vocab)
    hits = []
    for e in index.get("notes", []):
        score, matched = 0.0, None
        for name in e["names"]:
            parts = name.split()
            if not parts or (len(parts) == 1 and (len(name) < 3 or name in STOP)):
                continue
            if f" {name} " in joined:
                score = max(score, 100.0 * len(parts))
                matched = matched or name
        for t in terms:
            leaf = [tag.split("/")[-1] for tag in e["tags"]]
            if t in e["tags"] or t in leaf:
                score += 40
            if t in _norm_name(e["title"]) or t in e["desc"].lower():
                score += 10
        if score < 100:
            continue
        if e.get("type") == "project":
            score += 20
        top = e["p"].split("/")[0].lower()
        mult = {"1-projects": 1.0, "2-areas": 0.9, "3-resources": 0.9,
                "5-environment": 0.7, "0-inbox": 0.6, "4-archives": 0.25}.get(top, 0.85)
        score *= mult
        age = time.time() - e["mtime"]
        score *= 1.2 if age < 14 * 86400 else (0.8 if age > 180 * 86400 else 1.0)
        hits.append((score, matched, e))
    hits.sort(key=lambda h: -h[0])
    return [{"score": round(s, 1), "matched": m, **e} for s, m, e in hits[:MAX_NOTES]]


def _section(text: str, term: str) -> str:
    """The heading section around the first hit of `term`, khoj-style; else the head."""
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    if term:
        i = body.lower().find(term.split()[0])
        if i >= 0:
            start = body.rfind("\n#", 0, i)
            start = body.find("\n", start + 1) + 1 if start >= 0 else 0
            end = body.find("\n#", i)
            return body[start:end if end > 0 else len(body)].strip()
    return body.strip()


def context_for(cfg: dict, dictation: str, appdir, vocab="") -> tuple:
    """(xml_block, [{path, title}...]) from the cached index alone - the hot path never reads
    the mount (a live read through rclone cost seconds). ("", []) when the vault is off,
    unindexed, or nothing matches. Never raises."""
    try:
        vault = (cfg.get("vault_path") or "").strip()
        if not vault:
            return "", []
        idx = load_index(appdir)
        if not idx or idx.get("vault") != vault:
            return "", []
        picked = search(idx, dictation, vocab)
        if not picked:
            return "", []
        blocks, used, total = [], [], 0
        for e in picked:
            sec = _section(e.get("head") or "", e.get("matched") or "")
            parts = [x for x in (e.get("fm"), sec) if x]
            snippet = "\n".join(parts) if parts else e["desc"]
            snippet = snippet[:NOTE_BUDGET].replace("</note", "</ note")
            if total + len(snippet) > TOTAL_BUDGET:
                snippet = snippet[:max(0, TOTAL_BUDGET - total)]
            if not snippet:
                continue
            total += len(snippet)
            day = time.strftime("%Y-%m-%d", time.localtime(e["mtime"]))
            blocks.append(f'<note path="{e["p"]}" title="{e["title"]}" modified="{day}">\n{snippet}\n</note>')
            used.append({"path": e["p"], "title": e["title"]})
        if not blocks:
            return "", []
        return "<vault_context>\n" + "\n".join(blocks) + "\n</vault_context>\n", used
    except Exception as e:                          # the bridge must never break a draft
        log(f"  vault: context failed ({e})")
        return "", []


def obsidian_uri(vault_path: str, rel_path: str) -> str:
    name = Path(vault_path).name
    f = rel_path[:-3] if rel_path.lower().endswith(".md") else rel_path
    return f"obsidian://open?vault={urllib.parse.quote(name, safe='')}&file={urllib.parse.quote(f, safe='')}"
