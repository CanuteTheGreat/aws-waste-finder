"""Shared CLI contract (docs/CLI-CONTRACT.md): exit codes, --json schema, --quiet, stderr for errors.

Uses the moto AWS emulator fixture of the main test module; the tool runs as a subprocess.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import contract_support as cs
import test_aws_waste_finder as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import aws_waste_finder as tool  # noqa: E402

SCHEMA = cs.load_schema(ROOT, "aws-waste-finder")


def setUpModule():
    T.setUpModule()


def tearDownModule():
    T.tearDownModule()


class Contract(T.Base):
    def cli(self, *a, env=None):
        p = T.run(*a, env=env)
        return p.returncode, p.stdout, p.stderr

    def make_waste(self):
        self.ec2.allocate_address(Domain="vpc")


class TestExitCodes(Contract):
    def test_clean_is_0(self):
        self.assertEqual(self.cli()[0], 0)

    def test_findings_is_1(self):
        self.make_waste()
        self.assertEqual(self.cli()[0], 1)

    def test_min_savings_above_the_waste_is_0(self):
        self.make_waste()
        self.assertEqual(self.cli("--min-savings", "1000")[0], 0)

    def test_bad_args_is_2_with_message_on_stderr(self):
        for a in (("--no-such-flag",), ("--days", "many"), ("--region",)):
            rc, out, err = self.cli(*a)
            self.assertEqual(rc, 2, a)
            self.assertEqual(out, "", a)
            self.assertIn("usage:", err)

    def test_missing_prices_file_is_2(self):
        rc, out, err = self.cli("--prices", os.path.join(tempfile.gettempdir(), "no-such-prices.json"))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--prices", err)

    def test_aws_not_configured_keeps_documented_legacy_code_2(self):
        rc, out, err = self.cli(env=T.base_env(AWS_DEFAULT_REGION=None))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no AWS region configured", err)

    def test_unexpected_error_is_3_on_stderr_not_a_traceback_exit_1(self):
        buf = io.StringIO()
        with mock.patch.object(tool, "_main", side_effect=RuntimeError("boom")), redirect_stderr(buf):
            self.assertEqual(tool.main([]), 3)
        self.assertIn("RuntimeError: boom", buf.getvalue())


class TestJson(Contract):
    def check(self, code):
        rc, out, err = self.cli("--json")
        self.assertEqual(rc, code, err)
        doc = json.loads(out)  # exactly one JSON document, nothing else on stdout
        self.assertEqual(cs.validate(doc, SCHEMA), [])
        self.assertEqual(doc["exit_code"], rc)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["tool"], "aws-waste-finder")
        self.assertEqual(doc["version"], tool.__version__)
        return doc

    def test_clean(self):
        self.assertEqual(self.check(0)["estimated_monthly_usd"], 0)

    def test_findings(self):
        self.make_waste()
        self.assertGreater(self.check(1)["estimated_monthly_usd"], 0)

    def test_usage_error_leaves_stdout_empty(self):
        rc, out, err = self.cli("--json", "--no-such-flag")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage:", err)

    def test_schema_rejects_a_document_without_the_contract_keys(self):
        self.assertNotEqual(cs.validate({"regions": {}}, SCHEMA), [])
        good = {"schema_version": 1, "tool": "aws-waste-finder", "version": "1", "exit_code": 0, "regions": {},
                "region_errors": {}, "estimated_monthly_usd": 0, "prices": "x"}
        self.assertEqual(cs.validate(good, SCHEMA), [])
        self.assertNotEqual(cs.validate(dict(good, exit_code=9), SCHEMA), [])


class TestQuiet(Contract):
    def test_quiet_prints_nothing_but_keeps_exit_code(self):
        for waste, code in ((False, 0), (True, 1)):
            if waste:
                self.make_waste()
            for extra in ((), ("--json",)):
                for flag in ("--quiet", "-q"):
                    rc, out, err = self.cli(flag, *extra)
                    self.assertEqual((rc, out), (code, ""), (flag, extra, waste))

    def test_without_quiet_the_report_is_printed(self):
        self.assertIn("Total:", self.cli()[1])


if __name__ == "__main__":
    unittest.main()
