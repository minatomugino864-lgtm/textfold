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
    if ws.get("id") != wid or not (isinstance(ws.get("papers"), list) and isinstance(ws.get("relations"), list)
                                   and isinstance(ws.get("name"), str) and all(_relation_ok(r) for r in ws["relations"])):
        return None   # not a shape this version reads; the file is left as it is, for looking at
    return ws


def _relation_ok(r):
    """What a relation has to carry to be one of this version's: its kind, and both ends as anchors."""
    return (isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("type"), str)
            and all(isinstance(a, dict) and isinstance(a.get("paper"), str) and isinstance(a.get("unit"), str)
                    for a in (r.get("from"), r.get("to"))))


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
        rel = {"id": f"r-{uuid.uuid4().hex[:8]}", "type": rtype, "status": "user", "origin": "user",
               "from": frm, "to": to,
               "note": str(note or "").strip()[:2000], "created": _now(), "updated": _now()}
        ws["relations"].append(rel)
        _write(ws)
        return rel


def origin_of(r):
    """Who proposed a relation: the reader ("user"), or a model — with the reader holding either one only
    since it was reviewed. An old relation without the field reads by its status, as it was written."""
    return r.get("origin") or ("model" if r.get("status") == "suggested" else "user")


def accept_relation(ws, rid):
    """A suggested relation the reader has confirmed: it becomes one the reader holds — and stays what it is,
    a model's proposal the reader stands behind (origin and suggested_by keep that, reviewed says when)."""
    with _LOCK:
        for r in ws["relations"]:
            if r["id"] == rid:
                if r.get("status") != "suggested":
                    raise ValueError("not a suggestion")
                r["status"] = "user"
                r.setdefault("origin", "model")   # (an old suggestion was the model's, as this one is)
                r["reviewed"] = {"when": _now()}
                r["updated"] = _now()
                _write(ws)
                return r
        raise ValueError("no such relation")


def _dismiss_key(r):
    return f"{r['type']}|{r['from']['paper']}|{r['from']['unit']}|{r['to']['paper']}|{r['to']['unit']}"


def remove_relation(ws, rid):
    """A relation taken back. A suggestion the reader rejected is remembered by what it proposed (DISMISS_KEEP
    of them), so the next run of suggest does not offer the same relation again; a user's own is simply gone."""
    with _LOCK:
        if not RID_RE.fullmatch(rid or ""):
            raise ValueError("no such relation")
        kept = ws["relations"]
        gone = next((r for r in kept if r["id"] == rid), None)
        ws["relations"] = [r for r in kept if r["id"] != rid]
        if gone is None:
            raise ValueError("no such relation")
        if gone.get("status") == "suggested":
            ws["dismissed"] = (ws.get("dismissed") or [])[-DISMISS_KEEP + 1:] + [
                {"key": _dismiss_key(gone), "when": _now()}]
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
            who = r.get("suggested_by") or {}
            how = f", proposed by {who.get('model') or 'a model'}" if origin_of(r) == "model" else ""
            stood = f", confirmed {r['reviewed']['when']}" if (r.get("reviewed") or {}).get("when") else ""
            L.append(f"### {r['type']} — `{r['id']}` ({r.get('status') or 'user'}{how}{stood})\n")
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
VERIFY_CAP = 6             # evidence calls a run may attempt: 1 candidates call + 6 evidence calls, at most
RUN_CALLS = 1 + VERIFY_CAP # the run's budget in calls ATTEMPTED — a call that failed spent it too
QUOTE_MIN = 24             # a quotation too short proves little, and is refused as its own reason
DISMISS_KEEP = 200         # rejections a workspace remembers (bounded)
SUGGEST_TYPES = ("extends", "supports", "qualifies", "shares_method", "cites")   # (contradicts is not offered)



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


def _evidence_span(quote, full):
    """(span, verdict): the model's quotation found in the very text gives the text's own words to keep — its
    casing, its spacing, exactly as the paper writes it. An empty or unfound quotation is invented ("fabricated");
    one found but too short to prove anything is refused as its own reason, not as an invention."""
    q = " ".join((quote or "").split())
    if not q:
        return None, "fabricated_quote"
    m = re.search(r"\s+".join(map(re.escape, q.split())), full or "", re.I)
    if not m:
        return None, "fabricated_quote"
    if len(q) < QUOTE_MIN:
        return None, "quote_too_short"
    return m.group(0), None


def suggest(ws, papers=None, cfg=None, cache_dir=None, log=lambda *a: None):
    """A run of model proposals, as the plan bounds it: one candidates call over the takeaways, then one call
    per candidate for verbatim quotations, each checked against the quoted text. What survives lands as
    "suggested" with the model's name on it; what does not lands in the ledger, with the reason. Returns
    (workspace, ledger).

    The model calls run without any lock held — a run takes minutes and the reader is still working. The
    workspace on disk is read fresh at the end and what the run earned is merged into THAT: a relation the
    reader drew while the model was thinking is kept, never overwritten. The run's budget is calls attempted,
    1 + VERIFY_CAP at most, failures spent too; one bad reply drops its candidate, never the run."""
    from . import llm
    if cfg is None:
        raise RuntimeError("no model")
    papers = papers or [p["id"] for p in ws["papers"]]
    papers = [p for p in papers if p in {q["id"] for q in ws["papers"]}]
    if len(papers) < 2:
        raise ValueError("a suggestion wants two papers in the workspace at least")
    ledger = {"candidates": 0, "kept": 0, "calls": 0, "cost": 0.0, "dropped": [], "added": []}
    try:
        rows = _unit_rows(papers)
    except Exception as e:
        raise ValueError(f"cannot read the papers' text: {e}")
    model_label = (cfg.get("id") if isinstance(cfg, dict) else None) or "a model"   # (whose proposal it is)
    system = llm.system_for({"title": ws["name"]})

    try:
        rec, cached = llm.call(_candidates_prompt(papers, rows), CANDIDATE_SCHEMA, cfg, cache_dir, False,
                               system=system)
    except Exception as e:   # the whole run's first call failing is a run with nothing to show, said so
        ledger["calls"] += 1
        ledger["dropped"].append({"reason": f"candidates_call_failed: {str(e)[:120]}"})
        return ws, ledger
    ledger["calls"] += 1
    ledger["cost"] += 0 if cached else (rec.get("cost") or 0)
    out = rec.get("out") if isinstance(rec, dict) else None
    if isinstance(out, str):   # a model that answered as JSON text rather than an object
        try:
            out = json.loads(out)
        except ValueError:
            out = None
    cands = (out or {}).get("candidates") if isinstance(out, dict) else None
    if not isinstance(cands, list):
        ledger["dropped"].append({"reason": "bad_reply: no candidates to read"})
        return ws, ledger
    cands = cands[:CANDIDATE_CAP]

    # what the run already knows is taken or refused (the disk gets its own check at the merge)
    have = {_dismiss_key(r) for r in ws["relations"]} | {d["key"] for d in ws.get("dismissed", []) if isinstance(d, dict)}
    rows_by_key = {(r["paper"], r["unit"]): r["full"] for pid in rows for r in rows[pid]}
    earned = []   # (type, from anchor, to anchor, from span, to span, note) — merged at the end, in the lock
    for c in cands:
        if not isinstance(c, dict):
            ledger["dropped"].append({"candidate": None, "reason": "bad_reply: a candidate that is not a shape"})
            continue
        ledger["candidates"] += 1
        parsed = {"type": c.get("type"), "from": c.get("from") or {}, "to": c.get("to") or {},
                  "why": c.get("why") or ""}
        if parsed["type"] not in SUGGEST_TYPES:
            ledger["dropped"].append({"candidate": parsed, "reason": "unknown_type"})
            continue
        try:
            f = _clean_anchor({"paper": parsed["from"].get("paper"), "unit": parsed["from"].get("unit"),
                               "section": "", "quote": {}})
            t = _clean_anchor({"paper": parsed["to"].get("paper"), "unit": parsed["to"].get("unit"),
                               "section": "", "quote": {}})
        except ValueError as e:
            ledger["dropped"].append({"candidate": parsed, "reason": f"bad_anchor: {e}"})
            continue
        key = "|".join((parsed["type"], f["paper"], f["unit"], t["paper"], t["unit"]))
        if key in have:
            ledger["dropped"].append({"candidate": parsed, "reason": "duplicate"})
            continue
        if (f["paper"], f["unit"]) not in rows_by_key or (t["paper"], t["unit"]) not in rows_by_key:
            ledger["dropped"].append({"candidate": parsed, "reason": "unknown_unit"})   # no text, no call spent
            continue
        if ledger["calls"] >= RUN_CALLS:   # the budget is attempts spent — failures count too
            ledger["dropped"].append({"candidate": parsed, "reason": "over_budget"})
            continue
        try:
            frec, cached = llm.call(_evidence_prompt(parsed, rows), EVIDENCE_SCHEMA, cfg, cache_dir, False,
                                    system=system)
        except Exception as e:   # one candidate's evidence call failing drops that candidate, not the run —
            ledger["calls"] += 1          # and it spent its share of the budget all the same
            ledger["cost"] += 0
            ledger["dropped"].append({"candidate": parsed, "reason": f"evidence_call_failed: {str(e)[:120]}"})
            continue
        ledger["calls"] += 1
        ledger["cost"] += 0 if cached else (frec.get("cost") or 0)
        fout = frec.get("out") if isinstance(frec, dict) else None
        if isinstance(fout, str):
            try:
                fout = json.loads(fout)
            except ValueError:
                fout = None
        fout = fout if isinstance(fout, dict) else {}
        fq, tq = fout.get("from_quote") or "", fout.get("to_quote") or ""
        fspan, fverdict = _evidence_span(fq, rows_by_key[(f["paper"], f["unit"])])
        tspan, tverdict = _evidence_span(tq, rows_by_key[(t["paper"], t["unit"])])
        if fverdict or tverdict:
            ledger["dropped"].append({"candidate": parsed, "reason": fverdict or tverdict})
            continue
        have.add(key)
        earned.append((parsed, f, t, fspan, tspan, str(fout.get("note") or parsed["why"]).strip()[:2000]))

    # the merge, in the short lock, into the workspace as it stands NOW: nothing the reader did is lost
    def merge(w):
        fresh = {_dismiss_key(r) for r in w["relations"]} | {d["key"] for d in w.get("dismissed", []) if isinstance(d, dict)}
        for parsed, f, t, fspan, tspan, note in earned:
            key = "|".join((parsed["type"], f["paper"], f["unit"], t["paper"], t["unit"]))
            if key in fresh:
                ledger["dropped"].append({"candidate": parsed, "reason": "duplicate"})   # drawn while we asked
                continue
            fresh.add(key)
            w["relations"].append({
                "id": f"r-{uuid.uuid4().hex[:8]}", "type": parsed["type"], "status": "suggested",
                "origin": "model", "suggested_by": {"model": model_label, "when": _now()},
                "from": {**f, "quote": {"exact": fspan[:300], "prefix": "", "suffix": ""}},
                "to": {**t, "quote": {"exact": tspan[:300], "prefix": "", "suffix": ""}},
                "note": note, "created": _now(), "updated": _now()})
            ledger["kept"] += 1
            ledger["added"].append(w["relations"][-1]["id"])
        _write(w)
        return w

    with _LOCK:
        fresh = load(ws["id"])
        if fresh is None:   # the workspace was deleted while the model was thinking: it stays deleted
            ledger["dropped"].append({"reason": "workspace_deleted_mid_run"})
            return ws, ledger
        ws = merge(fresh)
    return ws, ledger
