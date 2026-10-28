#!/usr/bin/env python3
"""The real-batch harness for issue #84's smaller step (DESIGN.md has the state machine, metrics and the
preregistered rules). Published copy: the paths assume this file sits in a clone of the TextFold fork, one
level above `textfold/`; create a virtualenv first (`python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`) — the local run used exactly that layout with the repo at hand.

    run_batch.py prepare              fresh data root; the papers fetched from arXiv; manifest.json
    run_batch.py warmup               en levels (ladder + links) built once — reported separately, never mixed
                                      into a strategy's numbers
    run_batch.py run M0|M1|M2         one strategy over the papers: its own data folder (a copy of the warmup
                                      state), its own server, /api/generate per paper, HTTP polling for the
                                      reader-visible state, JSONL events; M2 ends with the open-equivalent fill
    run_batch.py report               raw.jsonl + run files -> report tables

The model, provider and key come from the reader's own settings; no key is read or printed here.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]   # this branch lives in a clone of the TextFold fork: the repo root is the package
VENV_PY = REPO / ".venv" / "bin" / "python"
sys.path.insert(0, str(REPO))

ROOT = Path("/tmp/tf-bf-batch")
PORT = 3211
PAPERS = ["2310.01704", "2310.06117", "2308.00352"]   # low / mid / high appendix share (GA excluded: budget)
LANG = "zh"
T0 = time.monotonic()

EVENTS = HERE / "raw.jsonl"


def event(run, paper, kind, detail=None):
    line = {"t_rel": round(time.monotonic() - T0, 1), "run": run, "paper": paper, "event": kind}
    if detail is not None:
        line["detail"] = detail
    with EVENTS.open("a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(json.dumps(line, ensure_ascii=False), flush=True)


def api(port, path, payload=None, tries=3):
    for attempt in range(tries):
        try:
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2)


def start_server(data_dir, knob=None):
    env = {**os.environ, "TEXTFOLD_DATA": str(data_dir)}
    if knob:
        env["TEXTFOLD_BODY_FIRST"] = knob
    log = open(data_dir / "server.log", "ab")
    proc = subprocess.Popen([str(VENV_PY), "-m", "textfold", "serve", "--port", str(PORT)],
                            cwd=str(REPO), env=env, stdout=log, stderr=log)
    for _ in range(120):
        try:
            api(PORT, "/api/papers")
            return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError("server did not come up")


def call_count(data_dir, pid):
    d = data_dir / "papers" / pid / "llm-cache"
    return sum(1 for _ in d.glob("*")) if d.exists() else 0


# ── the harness reads the same source the server parses: which units are body (offline, deterministic) ──
def body_ids_of(pid, data_dir):
    from textfold.parse import parse
    d = data_dir / "papers" / pid
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    doc = parse((d / "source.html").read_text(encoding="utf-8"), meta)
    return [uid for c in doc["chunks"] if c.get("app") is not True for uid in c["units"]]


def appendix_ids_of(pid, data_dir):
    from textfold.parse import parse
    d = data_dir / "papers" / pid
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    doc = parse((d / "source.html").read_text(encoding="utf-8"), meta)
    return [uid for c in doc["chunks"] if c.get("app") is True for uid in c["units"]]


def unit_texts(doc):
    return {u["id"]: u["full"] for sec in doc["sections"] for u in sec["units"]}


def prepare():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    (ROOT / "warmup").mkdir(parents=True)
    os.environ["TEXTFOLD_DATA"] = str(ROOT / "warmup")
    from textfold import store
    from textfold.fetch import fetch as fetch_paper
    store.DATA = ROOT / "warmup"
    manifest = {"papers": {}, "lang": LANG, "date": time.strftime("%Y-%m-%d"), "model": "(the reader's configured one)"}
    for pid in PAPERS:
        fetch_paper(pid, store.library_root())
        d = store.pdir(pid)
        html = (d / "source.html").read_text(encoding="utf-8")
        from textfold.parse import parse
        doc = parse(html, json.loads((d / "meta.json").read_text(encoding="utf-8")))
        units = len(doc["units"])
        app_units = sum(len(c["units"]) for c in doc["chunks"] if c.get("app"))
        words = lambda uid: sum(len((tk.get("t") or "").split()) for tk in doc["units"][uid]["toks"])
        w_total, w_app = sum(words(uid) for uid in doc["units"]), sum(words(uid) for c in doc["chunks"] if c.get("app") for uid in c["units"])
        chunks = len([c for c in doc["chunks"] if c["units"]])
        app_chunks = len([c for c in doc["chunks"] if c.get("app") and c["units"]])
        manifest["papers"][pid] = {
            "units": units, "appendix_units": app_units, "appendix_unit_share": round(app_units / units, 3),
            "words": w_total, "appendix_words": w_app, "appendix_word_share": round(w_app / w_total, 3),
            "chunks_with_units": chunks, "appendix_chunks": app_chunks,
            "source_sha16": hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]}
        print(pid, manifest["papers"][pid], flush=True)
    (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("manifest written", flush=True)


def warmup():
    os.environ["TEXTFOLD_DATA"] = str(ROOT / "warmup")
    proc = start_server(ROOT / "warmup")
    try:
        for pid in PAPERS:
            event("warmup", pid, "job_start_en")
            job = api(PORT, "/api/generate", {"paper": pid})   # no lang: the text's own levels
            t0 = time.monotonic()
            while True:
                time.sleep(3)
                jobs = api(PORT, "/api/jobs")
                j = next((x for x in jobs if x["id"] == job["id"]), None)
                if j and j["status"] in ("done", "error", "stopped"):
                    event("warmup", pid, f"job_{j['status']}", round(time.monotonic() - t0, 1))
                    break
                if time.monotonic() - t0 > 2400:
                    raise RuntimeError(f"warmup stalled for {pid}")
    finally:
        proc.terminate(); proc.wait(timeout=30)


def run(strategy, only_paper=None):
    knob = {"M0": None, "M1": "order", "M2": "skip"}[strategy]
    papers = [only_paper] if only_paper else PAPERS
    data_dir = ROOT / (f"run-{strategy}" + (f"-{only_paper}" if only_paper else ""))
    if data_dir.exists():
        shutil.rmtree(data_dir)
    shutil.copytree(ROOT / "warmup", data_dir)   # the pre-warmed levels: identical start for every strategy
    os.environ["TEXTFOLD_DATA"] = str(data_dir)
    proc = start_server(data_dir, knob)
    run_file = HERE / (f"run-{strategy}.json" + (f"-{only_paper}" if only_paper else ""))
    out = {"strategy": strategy, "knob": knob, "papers": {}}
    try:
        for pid in papers:
            event(strategy, pid, "job_start_zh")
            t0 = time.monotonic()
            job = api(PORT, "/api/generate", {"paper": pid, "lang": LANG})
            body_ids = body_ids_of(pid, data_dir)
            orig = unit_texts(api(PORT, f"/api/doc/{pid}"))
            last_stage, gaps_seen, persisted_at = None, None, None
            while True:
                time.sleep(2)
                jobs = api(PORT, "/api/jobs")
                j = next((x for x in jobs if x["id"] == job["id"]), None)
                if j and j["stage"] != last_stage:
                    event(strategy, pid, f"job_stage_{j['stage']}", f"{j.get('done')}/{j.get('total')}")
                    last_stage = j["stage"]
                doc = api(PORT, f"/api/doc/{pid}?lang={LANG}")
                have = unit_texts(doc)
                translated = [uid for uid in body_ids
                              if " ".join(have.get(uid, "").split()) != " ".join(orig.get(uid, "").split())]
                if persisted_at is None and len(translated) == len(body_ids):
                    persisted_at = round(time.monotonic() - t0, 1)
                    event(strategy, pid, "body_readable_http", len(translated))
                ng = (doc.get("meta") or {}).get("gaps", {}).get(LANG)
                if ng != gaps_seen:
                    event(strategy, pid, "gaps_count", ng)
                    gaps_seen = ng
                if j is None or j["status"] in ("done", "error", "stopped"):
                    event(strategy, pid, "job_done", j["status"] if j else "vanished")
                    break
                if time.monotonic() - t0 > 2400:
                    raise RuntimeError(f"generation stalled for {pid}")
            # the appendix state right after the build, BEFORE any fill: /api/doc does not expose meta.gaps,
            # so the harness counts it from the units — an appendix unit is untranslated while its rendering
            # in the target language is still the text's own words
            doc = api(PORT, f"/api/doc/{pid}?lang={LANG}")
            have = unit_texts(doc)
            untranslated_appendix = [uid for uid in appendix_ids_of(pid, data_dir)
                                     if " ".join(have.get(uid, "").split()) == " ".join(orig.get(uid, "").split())]
            entry = {"body_persisted_s": persisted_at, "pipeline_done_s": round(time.monotonic() - t0, 1),
                     "calls": call_count(data_dir, pid), "appendix_untranslated_after_build": untranslated_appendix,
                     "job_status": j["status"] if j else "vanished"}
            if strategy == "M2":   # the reader opens the paper: fillGaps submits a generation (the same POST)
                event(strategy, pid, "fill_start_open_equivalent", len(untranslated_appendix))
                f0 = time.monotonic()
                api(PORT, "/api/generate", {"paper": pid, "lang": LANG})
                while True:
                    time.sleep(2)
                    doc = api(PORT, f"/api/doc/{pid}?lang={LANG}")
                    have = unit_texts(doc)
                    left = [uid for uid in appendix_ids_of(pid, data_dir)
                            if " ".join(have.get(uid, "").split()) == " ".join(orig.get(uid, "").split())]
                    if not left:
                        event(strategy, pid, "fill_done_all_translated")
                        break
                    if time.monotonic() - f0 > 1800:
                        raise RuntimeError(f"fill stalled for {pid}")
                entry["appendix_fill_s"] = round(time.monotonic() - f0, 1)
                entry["calls_after_fill"] = call_count(data_dir, pid)
            out["papers"][pid] = entry
            print(strategy, pid, json.dumps(entry), flush=True)
    finally:
        proc.terminate(); proc.wait(timeout=30)
        run_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def report():
    rows = []
    for strategy in ("M0", "M1", "M2"):
        f = HERE / f"run-{strategy}.json"
        if f.exists():
            rows.append(json.loads(f.read_text(encoding="utf-8")))
    (HERE / "report-data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        prepare()
    elif cmd == "warmup":
        warmup()
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "report":
        report()
    else:
        raise SystemExit("prepare | warmup | run M0|M1|M2 | report")
