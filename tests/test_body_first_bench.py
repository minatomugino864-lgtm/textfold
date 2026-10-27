"""The batch harness's own logic, without a model or a server: which units count as the harness's "body" when
it reads the served source (the same file, the same parse), across the shapes the corpus covers — a plain
section, an appendix with nested depths, and a plain section after the appendices. The harness's real runs
live in work/experiments/body-first-20261027/; this file holds the line they must not cross.

Run from the repo root:
    python3 -m unittest discover tests
"""
import unittest

from textfold.parse import parse

META = {"id": "2401.00021", "title": "Bench fixture", "authors": [], "date": "", "abs_url": "", "html_url": "", "license": ""}

HTML = """
<article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title">Results</h2>
<div class="ltx_para"><p class="ltx_p" data-u="b1">Body one.</p></div></section>
<section class="ltx_appendix" id="A1"><h2 class="ltx_title">Appendix</h2>
<div class="ltx_para"><p class="ltx_p" data-u="b2">Appendix one.</p></div>
<section class="ltx_subsection" id="A1.SS1"><h3 class="ltx_title">Deep</h3>
<div class="ltx_para"><p class="ltx_p" data-u="b3">Deep appendix.</p></div></section></section>
<section class="ltx_section" id="S2"><h2 class="ltx_title">Checklist</h2>
<div class="ltx_para"><p class="ltx_p" data-u="b4">Checklist body.</p></div></section>
</article>
"""


def body_ids(html):
    """What the harness computes from the served source: the units whose section the #79 ancestry says is not
    the appendix's — the set whose translation-complete moment the batch measures over HTTP."""
    doc = parse(html, dict(META))
    return [uid for c in doc["chunks"] if c.get("app") is not True for uid in c["units"]]


class Bench(unittest.TestCase):
    def test_body_ids_skip_the_appendix_and_its_depths_and_keep_the_checklist(self):
        self.assertEqual(body_ids(HTML), ["b1", "b4"])

    def test_a_paper_without_an_appendix_names_all_its_units(self):
        html = HTML.replace('class="ltx_appendix" id="A1"', 'class="ltx_section" id="A1"')
        self.assertEqual(body_ids(html), ["b1", "b2", "b3", "b4"])


if __name__ == "__main__":
    unittest.main()
