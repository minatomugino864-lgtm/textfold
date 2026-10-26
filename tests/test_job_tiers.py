"""The generation queue works body first: a paper's appendix, put off by a body-first generation, waits until no
paper's body is waiting for the queue — whatever order the requests came in.

Run from the repo root:
    python3 -m unittest discover tests
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from textfold import llm, pipeline, server as server_module, store


class Tiers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for obj, key, value in ((store, "DATA", Path(tmp.name)),
                                (server_module.models, "resolve", lambda *a: {"kind": "fake", "id": "fake"})):
            patch = mock.patch.object(obj, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.order = []
        self.gates = {}

    def generate(self):
        """pipeline.generate's shape, without a model: a body's generation waits for its paper's gate (the model
        calls under way), an appendix's goes through at once."""
        def gen(pid, lang, spec, **kw):
            self.order.append((pid, kw.get("scope"), "start"))
            if kw.get("scope") != "appendix" and pid in self.gates:
                self.gates[pid].wait(5)
            if kw.get("stop") is not None and kw["stop"].is_set():
                raise llm.Stopped()
            self.order.append((pid, kw.get("scope"), "end"))
        return gen

    def jobs(self):
        patch = mock.patch.object(pipeline, "generate", self.generate())
        patch.start()
        self.addCleanup(patch.stop)
        return server_module.Jobs()

    def hold(self, *pids):
        for pid in pids:
            self.gates[pid] = threading.Event()

    def wait(self, jobs, n, timeout=10):
        deadline = time.time() + timeout
        while len([j for j in jobs.items.values() if j["status"] == "done"]) < n and time.time() < deadline:
            time.sleep(0.02)

    def test_a_body_waits_behind_every_body_before_any_appendix(self):
        jobs = self.jobs()
        try:
            self.hold("2401.00001", "2401.00002", "2401.00003")
            jobs.submit("2401.00001", "zh", False, scope="body")    # under way, its calls waiting
            jobs.submit("2401.00002", "zh", False, scope="body")    # a batch-mate, queued
            self.gates["2401.00001"].set()   # the first body is readable: its appendix is put on the queue (low)
            self.wait(jobs, 1)
            jobs.submit("2401.00003", "zh", False, scope="body")    # a fifth paper arrives while an appendix waits
            self.gates["2401.00002"].set()
            self.gates["2401.00003"].set()
            self.wait(jobs, 6)
        finally:
            for gate in self.gates.values():
                gate.set()
        appendix = next(j for j in jobs.items.values() if j.get("scope") == "appendix")
        self.assertEqual(appendix["status"], "done")
        started = [i for i, (pid, scope, what) in enumerate(self.order) if what == "start"]
        ends = {pid: max(i for i, (p, s, what) in enumerate(self.order) if p == pid and what == "end" and s != "appendix")
                for pid in ("2401.00001", "2401.00002", "2401.00003")}
        first_appendix = min(i for i, (pid, scope, what) in enumerate(self.order) if scope == "appendix" and what == "start")
        for pid in ("2401.00001", "2401.00002", "2401.00003"):   # no appendix call before any body is done
            self.assertGreater(first_appendix, ends[pid], self.order)
        seq = [(pid, scope) for pid, scope, what in self.order if what == "start"]
        self.assertEqual(seq, [("2401.00001", "body"), ("2401.00002", "body"), ("2401.00003", "body"),
                               ("2401.00001", "appendix"), ("2401.00002", "appendix"),
                               ("2401.00003", "appendix")], self.order)   # every body, then every appendix

    def test_a_body_only_generation_leaves_the_appendix_to_the_reader(self):
        jobs = self.jobs()
        jobs.submit("2401.00004", "zh", False, scope="body-only")
        self.wait(jobs, 1)
        self.assertFalse([j for j in jobs.items.values() if j.get("scope") == "appendix"], jobs.items)

    def test_a_stopped_body_chains_nothing(self):
        jobs = self.jobs()
        try:
            self.hold("2401.00005")
            jobs.submit("2401.00005", "zh", False, scope="body")
            deadline = time.time() + 10
            while time.time() < deadline and not self.order:
                time.sleep(0.02)
            jobs.stop(jobs.list()[0]["id"])
            self.gates["2401.00005"].set()
            deadline = time.time() + 10
            while time.time() < deadline and jobs.list()[0]["status"] == "running":
                time.sleep(0.02)
        finally:
            for gate in self.gates.values():
                gate.set()
        self.assertEqual(jobs.list()[0]["status"], "stopped")
        self.assertFalse([j for j in jobs.items.values() if j.get("scope") == "appendix"], jobs.items)


if __name__ == "__main__":
    unittest.main()
