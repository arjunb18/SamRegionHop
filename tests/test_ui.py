# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Arjun Bhagavatula
"""Tests for the local UI server, driving a stand-in CLI (no phone needed)."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ui  # noqa: E402

ALL_CHECKED = {"backup": True, "passwords": True, "firmware": True}


class UITest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ui, "CLI", os.path.join(HERE, "fake_cli.py"))
        p.start()
        self.addCleanup(p.stop)
        self.server, self.url, self.state = ui.serve(port=0, open_browser=False)
        self.base = self.url.split("/?")[0]
        self.token = self.url.split("t=")[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, body=None, token=None, headers=None):
        h = {"X-Token": self.token if token is None else token}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, body.decode()

    def wait(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            s = self.call("/state?since=0")[1]
            if pred(s):
                return s
            time.sleep(0.05)
        self.fail(f"timed out; last state {s}")

    def run_step(self, kind, csc="INS", checklist=None):
        return self.call("/run", {"kind": kind, "csc": csc, "checklist": checklist or {}})

    # -- access control -----------------------------------------------------

    def test_page_needs_token(self):
        with urllib.request.urlopen(self.url, timeout=5) as r:
            self.assertIn("SamRegionHop", r.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/?t=wrong", timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_api_needs_token(self):
        self.assertEqual(self.call("/state", token="wrong")[0], 403)
        self.assertEqual(self.call("/run", {"kind": "info", "csc": "INS"}, token="")[0], 403)

    def test_foreign_origin_and_host_rejected(self):
        self.assertEqual(self.call("/run", {"kind": "info", "csc": "INS"},
                                   headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("/state", headers={"Host": "evil.example"})[0], 403)

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    # -- steps --------------------------------------------------------------

    def test_info_runs_and_streams_output(self):
        self.assertEqual(self.run_step("info")[0], 200)
        s = self.wait(lambda s: s.get("code") == 0)
        self.assertIn("Active CSC", s["text"])

    def test_bad_inputs_rejected(self):
        self.assertEqual(self.run_step("dryrun", csc="IN;S")[0], 400)
        self.assertEqual(self.run_step("fota", csc="")[0], 400)
        self.assertEqual(self.run_step("rm -rf")[0], 409)

    def test_targets_and_report_need_no_region(self):
        self.assertEqual(self.call("/run", {"kind": "targets"})[0], 200)
        s = self.wait(lambda s: s.get("code") == 0)
        self.assertIn("Reliable targets: EUX INS", s["text"])
        self.assertEqual(self.call("/run", {"kind": "report"})[0], 200)
        s = self.wait(lambda s: s.get("kind") == "report" and s.get("code") == 0)
        self.assertIn("Report saved", s["text"])

    def test_regions_endpoint(self):
        code, r = self.call("/regions")
        self.assertEqual(code, 200)
        self.assertIn("India", r["INS"])
        self.assertGreater(len(r), 500)
        self.assertEqual(self.call("/regions", token="wrong")[0], 403)

    def test_one_step_at_a_time_and_stop(self):
        self.run_step("probe")
        self.wait(lambda s: "probing" in s.get("text", ""))
        self.assertEqual(self.run_step("info")[0], 409)
        self.call("/stop", {})
        s = self.wait(lambda s: s.get("running") is False)
        self.assertNotEqual(s["code"], 0)
        self.assertIn("interrupted", s["text"])

    # -- change gates -------------------------------------------------------

    def test_change_needs_dry_run_first(self):
        code, r = self.run_step("change", checklist=ALL_CHECKED)
        self.assertEqual(code, 409)
        self.assertIn("dry run", r["error"])

    def test_failed_dry_run_does_not_unlock(self):
        self.run_step("dryrun", csc="BAD")
        self.wait(lambda s: s.get("code") == 1)
        self.assertEqual(self.run_step("change", csc="BAD", checklist=ALL_CHECKED)[0], 409)

    def test_dry_run_for_other_csc_does_not_unlock(self):
        self.run_step("dryrun", csc="EUX")
        self.wait(lambda s: s.get("code") == 0)
        self.assertEqual(self.run_step("change", csc="INS", checklist=ALL_CHECKED)[0], 409)

    def test_change_needs_full_checklist(self):
        self.run_step("dryrun")
        self.wait(lambda s: s.get("code") == 0 and "INS" in s["dryrun_ok"])
        for partial in ({}, {"backup": True}, {"backup": True, "passwords": True},
                        {"backup": True, "passwords": True, "firmware": "yes"}):
            self.assertEqual(self.run_step("change", checklist=partial)[0], 409, partial)

    def test_change_passes_typed_confirmation_through(self):
        self.run_step("dryrun")
        self.wait(lambda s: s.get("code") == 0)
        self.assertEqual(self.run_step("change", checklist=ALL_CHECKED)[0], 200)
        self.wait(lambda s: s.get("awaiting") == "INS")
        self.assertEqual(self.call("/input", {"text": "INS"})[0], 200)
        s = self.wait(lambda s: s.get("code") is not None)
        self.assertEqual(s["code"], 0)
        self.assertIn("CHANGED to INS", s["text"])
        self.assertNotIn("INS", s["dryrun_ok"])  # next attempt needs a fresh dry run

    def test_cancel_at_confirmation_changes_nothing(self):
        self.run_step("dryrun")
        self.wait(lambda s: s.get("code") == 0)
        self.run_step("change", checklist=ALL_CHECKED)
        self.wait(lambda s: s.get("awaiting") == "INS")
        self.call("/input", {"text": ""})
        s = self.wait(lambda s: s.get("code") is not None)
        self.assertEqual(s["code"], 1)
        self.assertIn("Nothing was changed", s["text"])

    def test_input_only_accepted_while_prompting(self):
        self.assertEqual(self.call("/input", {"text": "INS"})[0], 409)
        self.run_step("info")
        self.wait(lambda s: s.get("code") == 0)
        self.assertEqual(self.call("/input", {"text": "INS"})[0], 409)

    def test_input_cannot_smuggle_extra_lines(self):
        self.run_step("dryrun")
        self.wait(lambda s: s.get("code") == 0)
        self.run_step("change", checklist=ALL_CHECKED)
        self.wait(lambda s: s.get("awaiting") == "INS")
        self.call("/input", {"text": "X\nINS"})
        s = self.wait(lambda s: s.get("code") is not None)
        self.assertEqual(s["code"], 1)


if __name__ == "__main__":
    unittest.main()
