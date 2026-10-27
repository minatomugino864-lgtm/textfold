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
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote as urlquote

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


_LOCK = threading.RLock()   # one writer at a time: the server is threaded, the file is written whole


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
    with _LOCK:
        ws = {"format": FORMAT, "version": VERSION, "id": f"rw-{_new_id()}",
              "name": str(name or "").strip()[:200] or "Untitled workspace",
              "question": str(question or "").strip()[:2000], "created": _now(), "updated": _now(),
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
    if not (isinstance(ws.get("papers"), list) and isinstance(ws.get("relations"), list)
            and isinstance(ws.get("name"), str) and all(isinstance(r, dict) and isinstance(r.get("id"), str)
                                                        for r in ws["relations"])):
        return None   # not a shape this version reads; the file is left as it is, for looking at
    return ws


def update(wid, fn):
    """Load, change, write — all under the one lock (the server is threaded, and two requests at once must not
    drop one of the changes). fn changes the workspace in place; what it raises comes out of here. None when
    there is no such workspace."""
    with _LOCK:
        ws = load(wid)
        if ws is None:
            return None
        fn(ws)
        return ws


def summaries():
    """Every workspace as the list at the left of the page shows it, newest first."""
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
    with _LOCK:
        pid = str(pid or "").strip()
        if not store.valid_id(pid) or not (store.pdir(pid) / "meta.json").exists():
            raise ValueError("no such paper")
        if not any(p["id"] == pid for p in ws["papers"]):
            ws["papers"].append({"id": pid, "version": paper_version(pid), "added": _now()})
            _write(ws)
        return ws


def remove_paper(ws, pid):
    with _LOCK:
        ws["papers"] = [p for p in ws["papers"] if p["id"] != str(pid)]
        _write(ws)   # (relations stay: their anchors now name a paper outside the set, and the reader says so)
        return ws


def _clean_anchor(a):
    """An anchor as it is kept: the paper, the unit (and section, when known) and the quotation shown for it.
    A unit with no name anchors nothing; the fields are strings or the anchor is not one."""
    if not isinstance(a, dict) or not isinstance(a.get("paper"), str) or not isinstance(a.get("unit"), str):
        raise ValueError("bad anchor")
    unit = a["unit"].strip()
    if not unit:
        raise ValueError("bad anchor: no unit")
    section = a.get("section")
    if section is not None and not isinstance(section, str):
        raise ValueError("bad anchor: the section is not a name")
    quote = a.get("quote")
    if quote is not None and not isinstance(quote, dict):
        raise ValueError("bad anchor: the quotation is not one")
    quote = quote or {}
    return {"paper": a["paper"], "unit": unit[:200], "section": (section or "").strip()[:200],
            "quote": {k: str(quote.get(k) or "")[:300] for k in ("exact", "prefix", "suffix")}}


def add_relation(ws, rtype, frm, to, note=""):
    with _LOCK:
        if rtype not in REL_TYPES:
            raise ValueError("unknown relation")
        frm, to = _clean_anchor(frm), _clean_anchor(to)
        for a in (frm, to):
            if a["paper"] not in {q["id"] for q in ws["papers"]}:
                raise ValueError("paper not in the workspace")
        rel = {"id": f"r-{uuid.uuid4().hex[:8]}", "type": rtype, "status": "user",
               "from": frm, "to": to,
               "note": str(note or "").strip()[:2000], "created": _now(), "updated": _now()}
        ws["relations"].append(rel)
        _write(ws)
        return rel


def accept_relation(ws, rid):
    """A suggested relation the reader has confirmed: it becomes one the reader holds, as any other."""
    with _LOCK:
        for r in ws["relations"]:
            if r["id"] == rid:
                if r.get("status") != "suggested":
                    raise ValueError("not a suggestion")
                r["status"] = "user"
                r["updated"] = _now()
                _write(ws)
                return r
        raise ValueError("no such relation")


def remove_relation(ws, rid):
    with _LOCK:
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
    return f"textfold://open?paper={urlquote(a['paper'])}&u={urlquote(a['unit'])}&lang={urlquote(lang)}"


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
            L.append(f"### {r['type']} — `{r['id']}` ({r.get('status') or 'user'})\n")
            L.append(f"- {REL_TYPES.get(r['type'], 'a relation')}")
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


# ── a model's proposals ───────────────────────────────────────────────────────────────────────────────────────
# Deliberately the only road to a "suggested" relation, and a narrow one: the model sees takeaways and proposes
# candidates, then must quote each side's own text verbatim, and the quotation is checked against that text
# before anything is kept. A proposal never becomes a relation the reader holds until the reader confirms it.

CANDIDATE_CAP = 8          # candidates the first phase may propose
VERIFY_CAP = 6             # candidates the second phase may ask about: 1 + VERIFY_CAP calls per run, at most
SUGGEST_TYPES = ("extends", "supports", "qualifies", "shares_method", "cites")   # (contradicts is not offered)


def _norm(t):
    return " ".join((t or "").split()).casefold()


def _unit_rows(papers):
    """Every paper's units as the two phases read them: id, its own takeaway, its full text."""
    from . import fold
    out = {}
    for pid in papers:
        doc = fold.document(pid, None)
        rows = []
        for sec in doc["sections"]:
            for u in sec["units"]:
                if (u.get("full") or "").strip():
                    rows.append({"paper": pid, "unit": u["id"], "section": sec["id"],
                                 "takeaway": u.get("takeaway") or u["full"][:80], "full": u["full"]})
        out[pid] = rows
    return out


def _candidates_prompt(papers, rows):
    blocks = []
    for pid in papers:
        title = ""
        try:
            title = json.loads(store.read_text(store.pdir(pid) / "meta.json")).get("title") or ""
        except (OSError, ValueError):
            pass
        lines = "\n".join(f"  [{r['unit']}] {r['takeaway']}" for r in rows[pid])
        blocks.append(f"Paper `{pid}`: {title}\n{lines}")
    return f"""Research workspace: these papers are being read together.

{chr(10).join(blocks)}

Which relations hold BETWEEN these papers (never inside one paper)? A relation is anchored in one unit of each
paper, and must rest on what those two units say. Types:
- extends: one takes the other's method, setup or finding further
- supports: one's results give evidence for a claim made in the other
- qualifies: one limits, weakens or adds a caveat to the other's claim
- shares_method: both rest on the same load-bearing method, dataset or technique (not merely the same field)
- cites: one cites the other
Propose at most {CANDIDATE_CAP} candidates, the defensible ones only; none is also an answer.
Return JSON {{"candidates": [{{"type": "extends", "from": {{"paper": "…", "unit": "…"}}, "to": {{"paper": "…", "unit": "…"}}, "why": "…"}}]}}."""


CANDIDATE_SCHEMA = {"type": "object", "properties": {"candidates": {"type": "array", "items": {
    "type": "object", "properties": {"type": {"type": "string"},
    "from": {"type": "object", "properties": {"paper": {"type": "string"}, "unit": {"type": "string"}}},
    "to": {"type": "object", "properties": {"paper": {"type": "string"}, "unit": {"type": "string"}}},
    "why": {"type": "string"}}, "required": ["type", "from", "to", "why"]}}}, "required": ["candidates"]}

EVIDENCE_SCHEMA = {"type": "object", "properties": {
    "from_quote": {"type": "string"}, "to_quote": {"type": "string"}, "note": {"type": "string"}},
    "required": ["from_quote", "to_quote", "note"]}


def _evidence_prompt(cand, rows):
    f, t = cand["from"], cand["to"]
    ftext = next((r["full"] for r in rows[f["paper"]] if r["unit"] == f["unit"]), "")
    ttext = next((r["full"] for r in rows[t["paper"]] if r["unit"] == t["unit"]), "")
    return (f"A proposed relation between two passages of two papers: {cand['type']} — {cand.get('why', '')}\n\n"
            f"From `{f['paper']}` · `{f['unit']}`, its full text:\n"
            f"<<<\n{ftext}\n>>>\n\n"
            f"To `{t['paper']}` · `{t['unit']}`, its full text:\n"
            f"<<<\n{ttext}\n>>>\n\n"
            "Quote ONE sentence verbatim from EACH full text — exactly as it is written there, from the from-text "
            "and from the to-text — that supports this relation. If either text does not carry such a sentence, "
            "return empty strings for both quotes. Return JSON {\"from_quote\": \"…\", \"to_quote\": \"…\", "
            "\"note\": \"why the two quotations together support the relation, in one sentence\"}.")


def _verified(quote, full):
    """The quotation must be the text's own words: a whitespace-insensitive substring of the very unit."""
    q, f = _norm(quote), _norm(full)
    return bool(q) and len(q) >= 24 and q in f


def suggest(ws, papers=None, cfg=None, cache_dir=None, log=lambda *a: None):
    """A run of model proposals, as the plan bounds it: one candidates call over the takeaways, then one call
    per candidate for verbatim quotations, each checked against the quoted text. What survives lands as
    "suggested"; what does not lands in the ledger, with the reason. Returns (workspace, ledger)."""
    from . import llm
    if cfg is None:
        raise RuntimeError("no model")
    papers = papers or [p["id"] for p in ws["papers"]]
    papers = [p for p in papers if p in {q["id"] for q in ws["papers"]}]
    if len(papers) < 2:
        raise ValueError("a suggestion wants two papers in the workspace at least")
    ledger = {"candidates": 0, "kept": 0, "calls": 0, "cost": 0.0, "dropped": [], "added": []}
    rows = _unit_rows(papers)
    rec, cached = llm.call(_candidates_prompt(papers, rows), CANDIDATE_SCHEMA, cfg, cache_dir, False,
                           system=llm.system_for({"title": ws["name"]}))
    ledger["calls"] += 1
    ledger["cost"] += 0 if cached else (rec.get("cost") or 0)
    cands = (rec["out"].get("candidates") or [])[:CANDIDATE_CAP]
    have = {(r["type"], r["from"]["paper"], r["from"]["unit"], r["to"]["paper"], r["to"]["unit"])
            for r in ws["relations"]}
    kept = 0
    for c in cands:
        ledger["candidates"] += 1
        why = {"type": c.get("type"), "from": c.get("from") or {}, "to": c.get("to") or {}}
        if why["type"] not in SUGGEST_TYPES:
            ledger["dropped"].append({"candidate": why, "reason": "unknown_type"})
            continue
        try:
            f, t = _clean_anchor({"paper": why["from"].get("paper"), "unit": why["from"].get("unit"),
                                  "section": "", "quote": {}}), _clean_anchor({"paper": why["to"].get("paper"),
                                  "unit": why["to"].get("unit"), "section": "", "quote": {}})
        except ValueError as e:
            ledger["dropped"].append({"candidate": why, "reason": f"bad_anchor: {e}"})
            continue
        if (why["type"], f["paper"], f["unit"], t["paper"], t["unit"]) in have:
            ledger["dropped"].append({"candidate": why, "reason": "duplicate"})
            continue
        if kept >= VERIFY_CAP:
            ledger["dropped"].append({"candidate": why, "reason": "over_budget"})
            continue
        try:
            frec, cached = llm.call(_evidence_prompt(why, rows), EVIDENCE_SCHEMA, cfg, cache_dir, False,
                                    system=llm.system_for({"title": ws["name"]}))
        except Exception as e:   # one candidate's evidence call failing drops that candidate, not the run
            ledger["dropped"].append({"candidate": why, "reason": f"evidence_call_failed: {str(e)[:120]}"})
            continue
        ledger["calls"] += 1
        ledger["cost"] += 0 if cached else (frec.get("cost") or 0)
        out = frec["out"]
        fq, tq = out.get("from_quote") or "", out.get("to_quote") or ""
        ffull = next((r["full"] for r in rows[f["paper"]] if r["unit"] == f["unit"]), "")
        tfull = next((r["full"] for r in rows[t["paper"]] if r["unit"] == t["unit"]), "")
        if not (_verified(fq, ffull) and _verified(tq, tfull)):
            ledger["dropped"].append({"candidate": why, "reason": "fabricated_quote"})
            continue
        rel = {"id": f"r-{uuid.uuid4().hex[:8]}", "type": why["type"], "status": "suggested",
               "from": {**f, "quote": {"exact": fq.strip()[:300], "prefix": "", "suffix": ""}},
               "to": {**t, "quote": {"exact": tq.strip()[:300], "prefix": "", "suffix": ""}},
               "note": str(out.get("note") or why.get("why") or "").strip()[:2000],
               "created": _now(), "updated": _now()}
        ws["relations"].append(rel)
        have.add((why["type"], f["paper"], f["unit"], t["paper"], t["unit"]))
        ledger["kept"] += 1
        ledger["added"].append(rel["id"])
        kept += 1
    with _LOCK:
        _write(ws)
    return ws, ledger
