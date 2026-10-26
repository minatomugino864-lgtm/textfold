"""A body-first translation: the body is asked for, the appendix put off in the layer, the reader told honestly,
and a later generate asks for the appendix alone — the whole chain without a model, against a fake call.

Run from the repo root:
    python3 -m unittest discover tests
"""
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from textfold import ladder, llm, pipeline, server as server_module, store, translate

PAPER = """
<article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title"><span class="ltx_tag">1</span>Results</h2>
<div class="ltx_para"><p class="ltx_p">The method converges. It beats the baseline.</p></div></section>
<section class="ltx_section" id="S2"><h2 class="ltx_title"><span class="ltx_tag">2</span>Setup</h2>
<div class="ltx_para"><p class="ltx_p">We use two datasets. Both are public.</p></div></section>
<section class="ltx_appendix" id="A1"><h2 class="ltx_title"><span class="ltx_tag">A</span>Proofs</h2>
<div class="ltx_para"><p class="ltx_p">The proof is long. It takes three pages.</p></div></section>
</article>
"""
META = {"id": "2401.00004", "title": "Convergence", "authors": [], "date": "", "abs_url": "",
        "html_url": "", "license": ""}
PID = META["id"]
CFG = {"kind": "fake", "id": "fake", "label": "fake"}


class Fake:
    """llm.call's shape, without a model: each numbered sentence of a section's prompt comes back as itself, marked
    as read. calls records {"section": the section's heading in the prompt, "units": how many were asked}."""

    def __init__(self):
        self.calls = []

    def __call__(self, prompt, schema, cfg, cache_dir, force, system=None):
        if "Translate these titles" in prompt:   # the titles call, answered and not recorded
            lines = [line for line in prompt.strip().splitlines()[-10:]
                     if line and not line.startswith(("Paper:", "Translate", "Return JSON"))]
            return {"out": {"titles": [f"题：{line}" for line in lines]}, "cost": 0.0}, False
        units, cur = [], None
        for line in prompt.splitlines():
            if re.match(r"\[(U\d+)\]", line):
                cur = {"id": re.match(r"\[(U\d+)\]", line).group(1), "sentences": [],
                       "takeaway": "要点句子", "reason": "", "topic": "主题"}
                units.append(cur)
            elif cur is not None and (m := re.match(r"  \d+: (.+)", line)):
                cur["sentences"].append("译：" + m.group(1))
        section = next((line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("This section: ")), "")
        self.calls.append({"section": section, "units": len(units)})
        return {"out": {"section": {"title": "节", "takeaway": "节要点", "topic": "主题"}, "units": units}, "cost": 0.0}, False


def ladders(doc):
    """What ladder.build does, without a model (the fake call is for the translation only)."""
    for u in doc["units"].values():
        u["lad"] = ladder.local(u, doc["atoms"])


def worded_by_chunk(doc):
    return {c["id"]: [uid for uid in c["units"] if translate.worded(doc["units"][uid])] for c in doc["chunks"]}


class BodyFirstChain(unittest.TestCase):
    """Only the body asked, the appendix put off in the layer; a fresh assemble (a restart) says so honestly; the
    backfill asks for the appendix alone; nothing already translated is asked again."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(store, "DATA", Path(tmp.name) / "data")
        patch.start()
        self.addCleanup(patch.stop)
        d = store.DATA / "papers" / PID
        d.mkdir(parents=True)
        (d / "source.html").write_text(PAPER)
        (d / "meta.json").write_text(json.dumps(META))
        self.fake = Fake()

    def generate(self, scope=None, force=False):
        with mock.patch.object(pipeline, "fetch", lambda *a: store.pdir(PID)), \
             mock.patch.object(pipeline.ladder, "build", lambda doc, *a, **k: ladders(doc)), \
             mock.patch.object(pipeline.links, "build", lambda *a, **k: None), \
             mock.patch.object(pipeline, "task", lambda cfg, name: cfg), \
             mock.patch.object(llm, "call", self.fake):
            pipeline.generate(PID, "zh", CFG, force=force, scope=scope)

    def test_body_only_then_restart_then_backfill(self):
        self.generate(scope="body")
        tr = store.read(PID, "tr-zh")
        self.assertEqual(tr["deferred"], ["A1"])
        first = store.assemble(PID)   # the reader's view, from the layers alone (no model call)
        self.assertEqual(first["meta"]["deferred"]["zh"], ["A1"])
        self.assertNotIn("zh", first["meta"].get("gaps", {}))   # the appendix is not a failure
        by_chunk = worded_by_chunk(first)
        for uid in by_chunk["S1"] + by_chunk["S2"]:
            self.assertIn("zh", first["units"][uid].get("tr", {}))
        for uid in by_chunk["A1"]:
            self.assertNotIn("zh", first["units"][uid].get("tr", {}))
        asked = sorted(c["section"] for c in self.fake.calls)
        self.assertEqual(asked, ["1 Results", "2 Setup"])   # the appendix was never asked
        n_after_body = len(self.fake.calls)
        self.generate(scope="appendix")   # the backfill, as its own generation
        tr = store.read(PID, "tr-zh")
        self.assertNotIn("deferred", tr)   # nothing put off any more
        whole = store.assemble(PID)
        self.assertNotIn("deferred", whole["meta"])
        self.assertNotIn("gaps", whole["meta"])
        for uid in by_chunk["A1"]:
            self.assertIn("zh", whole["units"][uid].get("tr", {}))
        self.assertEqual([c["section"] for c in self.fake.calls[n_after_body:]], ["A Proofs"])  # it alone
        for uid in by_chunk["S1"] + by_chunk["S2"]:   # the body's own words kept
            self.assertEqual(whole["units"][uid]["tr"]["zh"], first["units"][uid]["tr"]["zh"])

    def test_a_whole_generation_is_what_it_was(self):
        self.generate()
        tr = store.read(PID, "tr-zh")
        self.assertNotIn("deferred", tr)
        whole = store.assemble(PID)
        self.assertNotIn("deferred", whole["meta"])
        for uid in [uid for c in whole["chunks"] for uid in c["units"]]:
            if translate.worded(whole["units"][uid]):
                self.assertIn("zh", whole["units"][uid].get("tr", {}))
        self.assertEqual(sorted(c["section"] for c in self.fake.calls), ["1 Results", "2 Setup", "A Proofs"])

    def test_the_next_generate_asks_for_nothing_it_has(self):
        self.generate(scope="body")
        self.generate(scope="appendix")
        n = len(self.fake.calls)
        self.generate()   # the language again, no force: read from the layers, no model call
        self.assertEqual(len(self.fake.calls), n, self.fake.calls)

    def test_a_failed_body_paragraph_is_a_gap_and_the_appendix_is_not(self):
        self.generate(scope="body")
        tr = store.read(PID, "tr-zh")
        doc = store.parsed(PID)
        failed = worded_by_chunk(doc)["S1"][0]
        del tr["units"][failed]   # a call that failed: the layer has no translation for it
        store.write(PID, "tr-zh", tr)
        en = store.read(PID, "en")
        store.apply_en(doc, en)
        put_off = set(tr["deferred"])
        store.apply_tr(doc, "zh", tr)
        self.assertEqual(translate.gaps(doc, "zh", put_off), [failed])   # the failure, alone
        n = len(self.fake.calls)
        with mock.patch.object(llm, "call", self.fake):
            translate.fill(doc, "zh", CFG, None, skip=put_off)   # as pipeline's fill branch does
        store.save_tr(doc, "zh", en["version"], "fake", deferred=[c["id"] for c in doc["chunks"] if c["id"] in put_off])
        again = store.assemble(PID)
        self.assertNotIn("gaps", again["meta"])
        self.assertIn("zh", again["units"][failed].get("tr", {}))
        self.assertEqual(self.fake.calls[n:], [{"section": "1 Results", "units": 1}])   # that paragraph alone, asked again

    def test_a_stale_deferred_id_hurts_nothing(self):
        self.generate(scope="body")
        tr = store.read(PID, "tr-zh")
        tr["deferred"] = ["gone-chunk", "A1"]   # a section the text no longer has (a PDF converted again)
        store.write(PID, "tr-zh", tr)
        whole = store.assemble(PID)
        self.assertEqual(whole["meta"]["deferred"]["zh"], ["A1"])   # what is really there, named
        self.assertNotIn("gaps", whole["meta"])


class SubmitScopes(unittest.TestCase):
    """The queue merges what a reader would mean as one request, and keeps scopes apart."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for obj, key, value in ((store, "DATA", Path(tmp.name)),
                                (server_module.models, "resolve", lambda *a: dict(CFG))):
            patch = mock.patch.object(obj, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        d = store.DATA / "papers" / PID
        d.mkdir(parents=True)
        (d / "source.html").write_text(PAPER)
        (d / "meta.json").write_text(json.dumps(META))
        self.ran = []
        self.gate = threading.Event()

        def gen(pid, lang, cfg, **kw):
            self.ran.append((pid, lang, kw.get("scope")))
            self.gate.wait(5)   # hold the queue while the test submits the rest

        patch = mock.patch.object(pipeline, "generate", gen)
        patch.start()
        self.addCleanup(patch.stop)

    def test_same_request_merges_and_scopes_differ(self):
        jobs = server_module.Jobs()
        try:
            a = jobs.submit(PID, "zh", False)
            self.assertEqual(jobs.submit(PID, "zh", False)["id"], a["id"])   # the same ask is that job
            self.assertNotEqual(jobs.submit(PID, "zh", False, scope="body")["id"], a["id"])
            self.assertNotEqual(jobs.submit(PID, "zh", False, scope="appendix")["id"], a["id"])
        finally:
            self.gate.set()
        deadline = time.time() + 10
        while len(self.ran) < 3 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(sorted((s or "") for _, _, s in self.ran), ["", "appendix", "body"], self.ran)


if __name__ == "__main__":
    unittest.main()
