"""vault: index building over a fake vault, spoken-name matching, scoring, exclusions, the
deadline-guarded snippet reads, the injected format, and the Obsidian URI.
Run: python tests/test_vault.py"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault as V

V.log = lambda s: None
root = Path(tempfile.mkdtemp())
vp = root / "para"
app = root / "app"


def note(rel, text):
    p = vp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


note("1-Projects/second-brain/README.md", """---
type: project
status: active
tags:
  - project
  - project/second-brain
---

# second-brain

A Drive folder that sorts whatever lands in it into dated folders.

## Shape

The sorter runs nightly; the folder is called Inbox.
""")
note("1-Projects/murmur/README.md", "---\ntype: project\ntags:\n  - project/murmur\n---\n\n# murmur\n\nA dictation app.\n")
note("3-Resources/tools/ripgrep.md", "---\ntype: resource\nkind: tool\ndescription: fast grep\ntags:\n  - kind/tool\n---\n\n# ripgrep\n\nFast search tool.\n")
note("2-Areas/journal/2026-01-01.md", "# private day\n\nnever index this\n")
note("4-Archives/old-session.md", "# second-brain session\n\nsecond-brain came up here long ago.\n")
note("0-Inbox/scratch.md", "# scratch\n\nnothing\n")
(vp / ".obsidian").mkdir()
(vp / ".obsidian" / "app.json").write_text("{}")

idx = V.build_index(str(vp), app, excludes=["0-Inbox"])
paths = [e["p"] for e in idx["notes"]]
assert "1-Projects/second-brain/README.md" in paths and "3-Resources/tools/ripgrep.md" in paths
assert not any("journal" in p for p in paths), paths            # private folders never indexed
assert not any(p.startswith("0-Inbox") for p in paths)          # user exclusions honoured
sb = next(e for e in idx["notes"] if "second-brain" in e["p"])
assert sb["title"] == "second-brain" and "second brain" in sb["names"] and sb["type"] == "project"
assert "project/second-brain" in sb["tags"] and sb["desc"].startswith("A Drive folder")
rg = next(e for e in idx["notes"] if "ripgrep" in e["p"])
assert rg["desc"] == "fast grep"                                # frontmatter description wins
assert V.load_index(app)["vault"] == str(vp)
print("index ok")

# --- matching: the index's names against spoken text -----------------------------------------
d = "kind of similarly to how we set up second brain, you know, and make it fast"
hits = V.search(idx, d)
assert hits and hits[0]["p"] == "1-Projects/second-brain/README.md", hits
assert hits[0]["matched"] == "second brain"
# the archived copy scores far lower than the project hub
archive = [h for h in hits if h["p"].startswith("4-Archives")]
assert not archive or archive[0]["score"] < hits[0]["score"] / 2
assert V.search(idx, "nothing relevant spoken here at all") == []
assert V.search(idx, "we should set the app up") == []          # stopword-ish unigrams never match
terms = V.extract_terms('use the vibe-check flow like "second brain" with RowList', vocab="murmur, ripgrep")
assert "vibe check" in terms and "second brain" in terms and "row list" in terms
print("search ok")

# --- snippets: deadline-guarded reads, budgets, the injected format --------------------------
cfg = {"vault_path": str(vp), "vault_exclude": ["0-Inbox"]}
ctx, used = V.context_for(cfg, d, app)
assert ctx.startswith("<vault_context>") and ctx.rstrip().endswith("</vault_context>")
assert '<note path="1-Projects/second-brain/README.md" title="second-brain"' in ctx
assert "sorts whatever lands in it" in ctx and used[0]["title"] == "second-brain"
assert len(ctx) < V.TOTAL_BUDGET + 400
# a hung read falls back to the index description within the deadline
_read = V._read_deadline
V._read_deadline = lambda path, deadline: (time.sleep(min(deadline + 0.2, 0.5)), None)[1]
t0 = time.monotonic()
ctx2, used2 = V.context_for(cfg, d, app)
assert time.monotonic() - t0 < V.IO_DEADLINE_S + 1.5
assert "A Drive folder that sorts" in ctx2                      # the desc floor still serves
V._read_deadline = _read
# off, unindexed, or no match -> empty, never an exception
assert V.context_for({"vault_path": ""}, d, app) == ("", [])
assert V.context_for({"vault_path": str(vp)}, "irrelevant words only", app) == ("", [])
assert V.context_for({"vault_path": str(root / "elsewhere")}, d, app) == ("", [])
print("context ok")

# --- refresh: mismatch deletes before rebuilding; single-flight ------------------------------
V.refresh_if_stale({"vault_path": str(vp), "vault_exclude": ["0-Inbox", "3-Resources"]}, app)
for _ in range(100):
    if not V._building["flag"].is_set():
        break
    time.sleep(0.05)
idx2 = V.load_index(app)
assert idx2 and not any(p.startswith("3-Resources") for p in [e["p"] for e in idx2["notes"]])
print("refresh ok")

# --- uri -------------------------------------------------------------------------------------
u = V.obsidian_uri(str(vp), "1-Projects/second-brain/README.md")
assert u == "obsidian://open?vault=para&file=1-Projects%2Fsecond-brain%2FREADME"
assert "%20" in V.obsidian_uri(str(vp), "notes/my note.md")
print("all vault tests passed")
