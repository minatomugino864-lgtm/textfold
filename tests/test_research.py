"""A research workspace: papers of the reader's choosing, and the relations between them, anchored in both
papers' own paragraphs. The whole chain, without a model: make a workspace, add two papers, draw a relation
between two passages, read it back cold, follow both anchors' links, change a paper's text and see the anchor
say so — and the edges: an empty workspace, a paper the library does not hold, a relation type that is not one.

Run from the repo root:
    python3 -m unittest discover tests
"""
import json
import os
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from textfold import ids, research, store

ROOT = Path(__file__).resolve().parent.parent

PAPER = """
<article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title"><span class="ltx_tag">1</span>Results</h2>
<div class="ltx_para"><p class="ltx_p">The first method converges quickly and beats the baseline.</p></div>
<div class="ltx_para"><p class="ltx_p">The second method is slower but generalizes to new domains.</p></div>
</section>
<section class="ltx_section" id="S2"><h2 class="ltx_title"><span class="ltx_tag">2</span>Setup</h2>
<div class="ltx_para"><p class="ltx_p">Both experiments run on the same public datasets and hardware.</p></div>
<div class="ltx_para"><p class="ltx_p">The appendix gives the proofs and the full hyperparameter tables.</p></div>
</section>
</article>
"""
SHORTER = """
<article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title"><span class="ltx_tag">1</span>Results</h2>
<div class="ltx_para"><p class="ltx_p">The text was revised; the old second paragraph is no longer here.</p></div>
</section>
</article>
"""
META = {"id": "2401.00010", "title": "Methods", "authors": [], "date": "", "abs_url": "", "html_url": "", "license": ""}


def unit_ids(html):
    from textfold.parse import parse
    return list(parse(html, dict(META))["units"])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Unit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(store, "DATA", Path(tmp.name) / "data")
        patch.start()
        self.addCleanup(patch.stop)

    def make_paper(self, pid, html=PAPER):
        d = store.DATA / "papers" / pid
        d.mkdir(parents=True)
        meta = dict(META, id=pid)
        (d / "source.html").write_text(html)
        (d / "meta.json").write_text(json.dumps(meta))

    def test_create_load_list_and_the_file_says_what_it_is(self):
        ws = research.create("Related work", "How do the two methods differ?")
        again = research.load(ws["id"])
        self.assertEqual(again["name"], "Related work")
        self.assertEqual(again["format"], "textfold/research-workspace")
        self.assertEqual([w["id"] for w in research.list()], [ws["id"]])
        self.assertFalse(list(research._path(ws["id"]).parent.glob("*.tmp")))   # the atomic write left nothing over

    def test_a_paper_joins_the_set_and_a_relation_names_both_sides(self):
        self.make_paper("2401.00010")
        self.make_paper("2401.00011")
        ws = research.create("W")
        research.add_paper(ws, "2401.00010")
        research.add_paper(ws, "2401.00011")
        rel = research.add_relation(ws, "extends",
                                    {"paper": "2401.00010", "unit": "u0", "section": "S1",
                                     "quote": {"exact": "The first method converges quickly", "prefix": "", "suffix": ""}},
                                    {"paper": "2401.00011", "unit": "u1", "section": "S1",
                                     "quote": {"exact": "The second method is slower but generalizes", "prefix": "", "suffix": ""}},
                                    note="The second paper builds on the first's convergence result.")
        again = research.load(ws["id"])   # cold, as the next session reads it
        self.assertEqual(len(again["relations"]), 1)
        self.assertEqual(again["relations"][0]["type"], "extends")
        self.assertEqual(again["relations"][0]["from"]["unit"], "u0")
        self.assertEqual(again["relations"][0]["to"]["unit"], "u1")
        self.assertEqual(again["relations"][0]["status"], "user")   # a relation the reader drew, not a model's
        self.assertEqual(rel["id"], again["relations"][0]["id"])
        self.assertEqual([p["id"] for p in again["papers"]], ["2401.00010", "2401.00011"])

    def test_the_edges_name_themselves(self):
        self.make_paper("2401.00010")
        ws = research.create("W")
        with self.assertRaises(ValueError):
            research.add_paper(ws, "2401.99999")        # the library does not hold it
        with self.assertRaises(ValueError):
            research.add_relation(ws, "refutes", {"paper": "2401.00010", "unit": "u0"},
                                  {"paper": "2401.00010", "unit": "u1"})   # not a relation the reader can draw
        research.add_paper(ws, "2401.00010")
        with self.assertRaises(ValueError):
            research.add_relation(ws, "extends", {"paper": "2401.00011", "unit": "u0"},
                                  {"paper": "2401.00010", "unit": "u1"})   # the other paper is not in the set
        with self.assertRaises(ValueError):
            research.remove_relation(ws, "r-deadbeef")

    def test_export_roundtrip_and_the_links_point_at_both_paragraphs(self):
        self.make_paper("2401.00010")
        self.make_paper("2401.00011")
        ws = research.create("W", "Q")
        for pid in ("2401.00010", "2401.00011"):
            research.add_paper(ws, pid)
        research.add_relation(ws, "supports",
                              {"paper": "2401.00010", "unit": "u0", "section": "S1", "quote": {"exact": "converges"}},
                              {"paper": "2401.00011", "unit": "u1", "section": "S1", "quote": {"exact": "generalizes"}})
        md = research.export_markdown(ws)
        self.assertIn("textfold://open?paper=2401.00010&u=u0", md)
        self.assertIn("textfold://open?paper=2401.00011&u=u1", md)   # both sides go back into the reader
        # the JSON export read back whole, under a new id: the same papers and relations, nothing lost
        data = json.loads(research.export_json(ws))
        data["id"] = "rw-" + "0" * 10
        (research.root() / data["id"]).mkdir(parents=True)
        (research.root() / data["id"] / "workspace.json").write_text(json.dumps(data), encoding="utf-8")
        again = research.load(data["id"])
        self.assertEqual([r["from"]["unit"] for r in again["relations"]], ["u0"])
        self.assertEqual([p["id"] for p in again["papers"]], ["2401.00010", "2401.00011"])
        self.assertIn("## Relations", research.export_markdown(again))

    def test_an_empty_workspace_is_a_workspace(self):
        ws = research.create("Empty")
        self.assertEqual(research.list()[0]["papers"], 0)
        self.assertEqual(research.list()[0]["relations"], 0)
        self.assertIn("None yet.", research.export_markdown(ws))
        self.assertIn('"relations": []', research.export_json(ws))

    def test_removing_a_paper_keeps_its_relations_saying_so(self):
        self.make_paper("2401.00010")
        self.make_paper("2401.00011")
        ws = research.create("W")
        for pid in ("2401.00010", "2401.00011"):
            research.add_paper(ws, pid)
        research.add_relation(ws, "cites", {"paper": "2401.00011", "unit": "u0"},
                              {"paper": "2401.00010", "unit": "u0"})
        research.remove_paper(ws, "2401.00010")
        again = research.load(ws["id"])
        self.assertEqual(len(again["relations"]), 1)   # not silently gone
        self.assertEqual([p["id"] for p in again["papers"]], ["2401.00011"])

    def test_an_anchor_that_loses_its_paragraph_says_so(self):
        self.make_paper("2401.00010")
        ws = research.create("W")
        research.add_paper(ws, "2401.00010")
        uids = unit_ids(PAPER)
        research.add_relation(ws, "supports", {"paper": "2401.00010", "unit": uids[0]},
                              {"paper": "2401.00010", "unit": uids[-1]})
        # the text revised: the old last paragraph is no longer in it
        (store.pdir("2401.00010") / "source.html").write_text(SHORTER)
        from textfold.parse import parse
        now = ids.lookup("2401.00010", parse(SHORTER, dict(META)), units=[uids[0], uids[-1]])
        self.assertTrue(now["units"][uids[0]].get("here"))        # the first passage is still named
        self.assertTrue(now["units"][uids[-1]].get("gone") or now["units"][uids[-1]].get("unknown"),
                        now["units"][uids[-1]])                   # the other one says so, and the reader shows it


class Server(unittest.TestCase):
    """The routes on a server of its own (its own HOME and data folder, no model), as the page calls them."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home, cls.data = Path(cls.tmp.name) / "home", Path(cls.tmp.name) / "data"
        for pid in ("2401.00010", "2401.00011"):
            d = cls.data / "papers" / pid
            d.mkdir(parents=True)
            (d / "source.html").write_text(PAPER)
            (d / "meta.json").write_text(json.dumps(dict(META, id=pid)))
        cls.port = free_port()
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData" / "Local"),
               "TEXTFOLD_DATA": str(cls.data), "TEXTFOLD_NO_MODELS": "1", "PYTHONUSERBASE": site.getuserbase()}
        cls.proc = subprocess.Popen([sys.executable, "-m", "textfold", "serve", "--port", str(cls.port)], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.call("/api/papers", want_code=False)
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    @classmethod
    def call(cls, path, body=None, want_code=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        out = json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode("utf-8", "replace")
        return (code, out) if want_code else out

    def test_the_whole_chain_over_the_routes(self):
        code, ws = self.call("/api/research", {"name": "Chain", "question": "How do they relate?"})
        self.assertEqual(code, 200)
        wid = ws["id"]
        code, ws = self.call(f"/api/research/{wid}", {"add_paper": "2401.00010"})
        self.assertEqual(code, 200)
        code, ws = self.call(f"/api/research/{wid}", {"add_paper": "2401.00011"})
        self.assertEqual(code, 200)
        uids = unit_ids(PAPER)
        code, got = self.call(f"/api/research/{wid}/relations",
                              {"type": "extends", "from": {"paper": "2401.00010", "unit": uids[0], "section": "S1",
                                                           "quote": {"exact": "The first method converges quickly"}},
                               "to": {"paper": "2401.00011", "unit": uids[1], "section": "S1",
                                      "quote": {"exact": "The second method is slower"}},
                               "note": "the second extends the first"})
        self.assertEqual(code, 200)
        rid = got["relation"]["id"]
        # the list says what is in it; the workspace read back cold keeps the relation
        listing = self.call("/api/research", want_code=False)
        mine = next(w for w in listing if w["id"] == wid)
        self.assertEqual(mine["relations"], 1)
        self.assertEqual(mine["papers"], 2)
        again = self.call(f"/api/research/{wid}", want_code=False)
        self.assertEqual(again["relations"][0]["id"], rid)
        # the exports
        code, md = self.call(f"/api/research/{wid}/export?format=md")
        self.assertEqual(code, 200)
        self.assertIn(f"textfold://open?paper=2401.00010&u={uids[0]}", md)
        code, js = self.call(f"/api/research/{wid}/export?format=json")
        self.assertEqual(js["relations"][0]["note"], "the second extends the first")
        # a relation the reader withdraws
        code, ws = self.call(f"/api/research/{wid}/relations", {"op": "remove", "id": rid})
        self.assertEqual(code, 200)
        self.assertEqual(self.call(f"/api/research/{wid}", want_code=False)["relations"], [])
        # and the page itself is served, apart from the reader
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/research", timeout=30) as r:
            self.assertIn("Research", r.read().decode("utf-8"))

    def test_the_routes_refuse_what_is_not_there(self):
        self.assertEqual(self.call("/api/research/rw-0000000000")[0], 404)
        self.assertEqual(self.call("/api/research/rw-0000000000/relations", {"type": "cites"})[0], 404)
        code, ws = self.call("/api/research", {"name": "W"})
        self.assertEqual(self.call(f"/api/research/{ws['id']}", {"add_paper": "2401.99999"})[0], 400)
        self.assertEqual(self.call(f"/api/research/{ws['id']}/relations", {"type": "refutes"})[0], 400)
        self.assertEqual(self.call("/api/research/rw-nope", want_code=True)[0], 404)


if __name__ == "__main__":
    unittest.main()
