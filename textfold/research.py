"""A research workspace: a set of papers and the relations a reader draws between them, each anchored in both
papers' own paragraphs, with a quotation to show for it. An independent place, entered on purpose — the reader's
five levels and its notes are untouched by it.

Workspaces live in `<data>/research/<id>/workspace.json`, written whole and atomically (a tmp file, renamed), as
the library keeps library.json. The format is versioned and the whole file exports and reads back as it is; the
Markdown export carries each anchor as a `textfold://open?…` link, so a relation can be followed from the export
back into the reader.

A relation is made by the reader ("status": "user"); a later one may be proposed by a model ("suggested") — and a
proposal never becomes a fact of its own. Anchors name a unit (and its section) of a paper as it stood when the
relation was made; when the text changes under them, the server's `/api/ids` says what each names now (or that it
is gone), and the reader says so rather than silently landing somewhere else. Deleting a paper from a set keeps
its relations: they show as broken, they do not disappear.
"""
import json
import re
import time
import uuid
from pathlib import Path

from . import store

FORMAT = "textfold/research-workspace"
VERSION = 1

# the relation types a reader draws, and what each means (the Markdown export spells them out)
REL_TYPES = {
    "extends": "one paper takes the other's method, setup or finding further",
    "supports": "one paper's results give evidence for a claim made in the other",
    "qualifies": "one paper limits, weakens or adds a caveat to the other's claim",
    "contrasts": "the two papers read the same question differently, under comparable settings",
    "shares_method": "both papers rest on the same method, dataset or technique",
    "cites": "one paper cites the other, as the bibliography says",
}
WID_RE = re.compile(r"rw-[0-9a-f]{10}")
RID_RE = re.compile(r"r-[0-9a-f]{8}")


def root():
    return Path(store.DATA) / "research"


def _path(wid):
    return root() / wid / "workspace.json"


def valid_id(wid):
    return bool(WID_RE.fullmatch(wid or ""))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_id():
    return uuid.uuid4().hex[:10]


def _write(ws):
    ws["updated"] = _now()
    f = _path(ws["id"])
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(ws, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def create(name, question=""):
    ws = {"format": FORMAT, "version": VERSION, "id": f"rw-{_new_id()}",
          "name": (name or "").strip()[:200] or "Untitled workspace",
          "question": (question or "").strip()[:2000], "created": _now(), "updated": _now(),
          "papers": [], "relations": []}
    _write(ws)
    return ws


def load(wid):
    if not valid_id(wid):
        return None
    f = _path(wid)
    if not f.exists():
        return None
    try:
        ws = json.loads(store.read_text(f))
    except (OSError, ValueError):
        return None
    if not isinstance(ws, dict) or ws.get("format") != FORMAT:
        return None
    return ws


def list():
    out = []
    for d in root().iterdir() if root().exists() else []:
        ws = load(d.name)
        if ws:
            out.append({"id": ws["id"], "name": ws["name"], "question": ws.get("question", ""),
                        "papers": len(ws["papers"]), "relations": len(ws["relations"]), "updated": ws["updated"]})
    return sorted(out, key=lambda w: w["updated"], reverse=True)


def paper_version(pid):
    en = store.read(pid, "en")
    return en["version"] if en else None


def add_paper(ws, pid):
    """A paper joins the set: it must be one the library holds (its text is what the anchors will point into)."""
    pid = (pid or "").strip()
    if not store.valid_id(pid) or not (store.pdir(pid) / "meta.json").exists():
        raise ValueError("no such paper")
    if not any(p["id"] == pid for p in ws["papers"]):
        ws["papers"].append({"id": pid, "version": paper_version(pid), "added": _now()})
        _write(ws)
    return ws


def remove_paper(ws, pid):
    ws["papers"] = [p for p in ws["papers"] if p["id"] != pid]
    _write(ws)   # (relations stay: their anchors now name a paper outside the set, and the reader says so)
    return ws


def _clean_anchor(a):
    """An anchor as it is kept: the paper, the unit (and section, when known) and the quotation shown for it."""
    if not isinstance(a, dict) or not isinstance(a.get("paper"), str) or not isinstance(a.get("unit"), str):
        raise ValueError("bad anchor")
    quote = a.get("quote") or {}
    return {"paper": a["paper"], "unit": a["unit"].strip()[:200], "section": (a.get("section") or "").strip()[:200],
            "quote": {k: str(quote.get(k) or "")[:300] for k in ("exact", "prefix", "suffix")}}


def add_relation(ws, rtype, frm, to, note=""):
    if rtype not in REL_TYPES:
        raise ValueError("unknown relation")
    for p in (frm, to):
        if p["paper"] not in {q["id"] for q in ws["papers"]}:
            raise ValueError("paper not in the workspace")
    rel = {"id": f"r-{uuid.uuid4().hex[:8]}", "type": rtype, "status": "user",
           "from": _clean_anchor(frm), "to": _clean_anchor(to),
           "note": (note or "").strip()[:2000], "created": _now(), "updated": _now()}
    ws["relations"].append(rel)
    _write(ws)
    return rel


def remove_relation(ws, rid):
    if not RID_RE.fullmatch(rid or ""):
        raise ValueError("no such relation")
    kept = ws["relations"]
    ws["relations"] = [r for r in kept if r["id"] != rid]
    if len(ws["relations"]) == len(kept):
        raise ValueError("no such relation")
    _write(ws)
    return ws


def export_json(ws):
    return json.dumps(ws, ensure_ascii=False, indent=1)


def _link(a, lang="en"):
    return f"textfold://open?paper={a['paper']}&u={a['unit']}&lang={lang}"


def export_markdown(ws, lang="en"):
    """The workspace as the reader's own notes would carry it: the papers, then every relation with both sides'
    quotation and a link back to the very paragraph. Reads into Obsidian; the JSON export is the machine's."""
    L = []
    L.append(f"# {ws['name']}\n")
    if ws.get("question"):
        L.append(f"**Question.** {ws['question']}\n")
    if ws["papers"]:
        L.append("## Papers\n")
        for p in ws["papers"]:
            meta = {}
            try:
                meta = json.loads(store.read_text(store.pdir(p["id"]) / "meta.json"))
            except (OSError, ValueError):
                pass
            L.append(f"- **{meta.get('title') or p['id']}** (`{p['id']}`)"
                     + (f" — textfold://open?paper={p['id']}&lang={lang}" if meta else ""))
        L.append("")
    if ws["relations"]:
        L.append("## Relations\n")
        for r in ws["relations"]:
            L.append(f"### {r['type']} — `{r['id']}` ({r['status']})\n")
            L.append(f"- {REL_TYPES[r['type']]}")
            for side in ("from", "to"):
                a = r[side]
                q = a["quote"].get("exact") or ""
                L.append(f"- **{side}** `{a['paper']}` · `{a['unit']}`: “{q}” — {_link(a, lang)}")
            if r.get("note"):
                L.append(f"- **Why.** {r['note']}")
            L.append("")
    else:
        L.append("## Relations\n\nNone yet.\n")
    return "\n".join(L)
