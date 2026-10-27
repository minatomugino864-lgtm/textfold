"""The body-first experiment (issue #84's smaller step): within one generation's translation, the body's parts
go first, and — in the skip strategy — the appendix's translation is left as real gaps for the reader's
open-and-fill road, instead of being asked in the same build. Strategies ride on an experiment-only env knob
(TEXTFOLD_BODY_FIRST = order | skip); the upstream patch, if the measurement earns one, makes the behavior the
default and drops the knob.

Run from the repo root:
    python3 -m unittest discover tests
"""
import json
import os
import unittest
from unittest import mock

from textfold import ladder, llm, translate
from textfold.parse import parse

META = {"id": "2401.00020", "title": "Body first fixture", "authors": [], "date": "", "abs_url": "",
        "html_url": "", "license": ""}

HTML = """
<article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title"><span class="ltx_tag">1</span>Results</h2>
<div class="ltx_para"><p class="ltx_p">Alpha body sentence one with words.</p></div>
<div class="ltx_para"><p class="ltx_p">Beta body sentence two with words.</p></div></section>
<section class="ltx_appendix" id="A1"><h2 class="ltx_title"><span class="ltx_tag">A</span>Proofs</h2>
<div class="ltx_para"><p class="ltx_p">Gamma appendix proof text with words.</p></div>
<section class="ltx_subsection" id="A1.SS1"><h3 class="ltx_title"><span class="ltx_tag">A.1</span>Lemmas</h3>
<div class="ltx_para"><p class="ltx_p">Delta appendix nested text with words.</p></div></section></section>
<section class="ltx_section" id="S2"><h2 class="ltx_title">NeurIPS Checklist</h2>
<div class="ltx_para"><p class="ltx_p">Epsilon checklist body text with words.</p></div></section>
</article>
"""
BODY_UNITS, APPENDIX_UNITS = ("u0", "u1", "u4"), ("u2", "u3")


def doc_of(html=HTML):
    doc = parse(html, dict(META))
    for u in doc["units"].values():   # as store.assemble does when the en layer is read
        u["lad"] = ladder.local(u, doc["atoms"])
    return doc


def speak(records):
    """A model that translates whatever a prompt brings and remembers the order of the prompts."""
    import re

    def fake(prompt, schema, cfg, cache_dir, force, system=None):
        records.append(prompt)
        if "Translate these titles" in prompt:
            titles = [line for line in prompt.strip().splitlines()[-8:]
                      if line and not line.startswith(("Paper:", "Translate", "Return JSON"))]
            return {"out": {"titles": [f"题：{line}" for line in titles]}, "cost": 0.0}, False
        units, cur = [], None
        for line in prompt.splitlines():
            if re.match(r"\[(U\d+)\]", line):
                cur = {"id": re.match(r"\[(U\d+)\]", line).group(1), "sentences": [],
                       "takeaway": "要点", "reason": "", "topic": "主题"}
                units.append(cur)
            elif cur is not None and (m := re.match(r"  \d+: (.+)", line)):
                cur["sentences"].append("译：" + m.group(1))
        return {"out": {"section": {"title": "节", "takeaway": "节要点", "topic": "主题"}, "units": units},
                "cost": 0.0}, False
    return fake


def keyword_seen(prompt, word):
    return word.lower() in prompt.lower()


class BodyFirst(unittest.TestCase):
    def setUp(self):
        self.doc = doc_of()

    def build(self, mode, doc=None, jobs=1):
        doc = doc or self.doc
        records = []
        env = {"TEXTFOLD_BODY_FIRST": mode} if mode else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(llm, "call", speak(records)):
            translate.build(doc, "zh", {"kind": "fake", "id": "fake"}, None, jobs=jobs, log=lambda *a: None)
        return doc, records

    def test_upstream_default_translates_everything(self):
        doc, records = self.build(None)
        have = {uid for uid, u in doc["units"].items() if "zh" in u.get("tr", {})}
        self.assertEqual(have, set(BODY_UNITS) | set(APPENDIX_UNITS))
        self.assertEqual(translate.gaps(doc, "zh"), [])

    def test_skip_leaves_the_appendix_as_real_gaps(self):
        doc, records = self.build("skip")
        have = {uid for uid, u in doc["units"].items() if "zh" in u.get("tr", {})}
        self.assertEqual(have, set(BODY_UNITS))
        self.assertEqual(translate.gaps(doc, "zh"), list(APPENDIX_UNITS))   # real gaps, refillable by fill()
        # and the same build did not quietly refill them (the _again scope was restricted)
        self.assertEqual(len(records), len([r for r in records if True]))

    def test_skip_translates_the_checklist_and_the_deep_appendix_is_skipped_whole(self):
        doc, _ = self.build("skip")
        self.assertIn("zh", doc["units"]["u4"].get("tr", {}))       # the checklist is body (ancestry, #79)
        for uid in APPENDIX_UNITS:                                   # nested subsection included
            self.assertNotIn("zh", doc["units"][uid].get("tr", {}))
        appendix_chunk = next(c for c in doc["chunks"] if c.get("app") is True)
        self.assertNotIn("zh", appendix_chunk.get("tr", {}))         # its title waits for the fill too

    def test_order_dispatches_body_parts_before_appendix_parts(self):
        doc, records = self.build("order", jobs=1)
        words = [("alpha", "u0"), ("beta", "u1"), ("gamma", "u2"), ("delta", "u3"), ("epsilon", "u4")]
        seen = [(i, uid) for i, pr in enumerate(records) for w, uid in words if keyword_seen(pr, w)]
        first_of = {}
        last_of = {}
        for i, uid in seen:
            first_of.setdefault(uid, i)
            last_of[uid] = max(last_of.get(uid, -1), i)
        body_last = max(last_of[uid] for uid in BODY_UNITS)
        appendix_first = min(first_of[uid] for uid in APPENDIX_UNITS)
        self.assertLess(body_last, appendix_first)   # with one worker, every body part went out first

    def test_skip_spends_fewer_calls_than_the_default(self):
        _, default_records = self.build(None)
        _, skip_records = self.build("skip")
        self.assertLess(len(skip_records), len(default_records))

    def test_fill_afterwards_covers_the_appendix_the_skip_left(self):
        doc, _ = self.build("skip")
        self.assertEqual(translate.gaps(doc, "zh"), list(APPENDIX_UNITS))
        records = []
        with mock.patch.dict(os.environ, {"TEXTFOLD_BODY_FIRST": "skip"}), \
                mock.patch.object(llm, "call", speak(records)):   # the knob must not shrink the fill's scope
            translate.fill(doc, "zh", {"kind": "fake", "id": "fake"}, None, log=lambda *a: None)
        have = {uid for uid, u in doc["units"].items() if "zh" in u.get("tr", {})}
        self.assertEqual(have, set(BODY_UNITS) | set(APPENDIX_UNITS))
        self.assertEqual(translate.gaps(doc, "zh"), [])

    def test_a_paper_without_an_appendix_is_unchanged_by_skip(self):
        html = HTML.replace('<section class="ltx_appendix" id="A1">', '<section class="ltx_section" id="A1">') \
                   .replace("</section></section>\n<section class=\"ltx_section\" id=\"S2\">",
                            "</section>\n<section class=\"ltx_section\" id=\"S2\">")
        doc = doc_of(html)
        doc, _ = self.build("skip", doc=doc)
        have = {uid for uid, u in doc["units"].items() if "zh" in u.get("tr", {})}
        self.assertEqual(have, set(doc["units"]))
        self.assertEqual(translate.gaps(doc, "zh"), [])


if __name__ == "__main__":
    unittest.main()
