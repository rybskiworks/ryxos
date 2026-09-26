#!/usr/bin/env python3
"""Validate generated settings with the selected Xen config-only parser."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BOOLEAN_KEYS = (
    "merge-activate",
    "conflict-rate-limit-is-aggregate",
    "perms-activate",
    "perms-watch-activate",
    "quota-activate",
)
OPTIONS = None


class XenstoreConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(OPTIONS.fixtures.read_text())
        if str(OPTIONS.parser) != cls.fixtures["parser"]:
            raise ValueError("The parser must match the evaluated Xen package")

    def parse(self, text):
        with tempfile.TemporaryDirectory(prefix="xenstore-config-") as temporary:
            root = Path(temporary)
            source = root / "oxenstored.conf"
            source.write_text(text)
            result = subprocess.run(
                [str(OPTIONS.parser), "--config-test", "--config-file", str(source)],
                cwd=root,
                env={"PATH": os.defpath, "HOME": str(root), "LC_ALL": "C"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertLess(len(result.stdout) + len(result.stderr), 16384)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [source.name])
            return result

    def replace_value(self, text, key, value):
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if line.startswith(key + " = ")]
        self.assertEqual(len(matches), 1, key)
        lines[matches[0]] = key + " = " + value
        return "\n".join(lines) + "\n"

    def test_evaluated_contract(self):
        self.assertGreaterEqual(len(self.fixtures["contract"]), 17)
        self.assertTrue(all(self.fixtures["contract"].values()))

    def test_selected_parser_accepts_all_generated_configurations(self):
        for name, text in self.fixtures["valid"].items():
            with self.subTest(configuration=name):
                result = self.parse(text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Configuration valid at ", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_former_numeric_and_empty_booleans_are_rejected(self):
        for key in BOOLEAN_KEYS:
            for value in ("1", ""):
                with self.subTest(key=key, value=value):
                    text = self.replace_value(self.fixtures["valid"]["enabled"], key, value)
                    result = self.parse(text)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("config: " + key + ": expect bool arg", result.stderr)
                    self.assertNotIn("Configuration valid at ", result.stdout)

    def test_misspelled_access_log_setting_is_rejected(self):
        text = self.fixtures["valid"]["enabled"]
        self.assertEqual(text.count("access-log-nb-chars = "), 1)
        result = self.parse(text.replace("access-log-nb-chars = ", "acesss-log-nb-chars = "))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config: unknown key acesss-log-nb-chars", result.stderr)

    def test_unrecognized_boolean_is_not_silently_accepted(self):
        text = self.replace_value(self.fixtures["valid"]["enabled"], "quota-activate", "invalid")
        result = self.parse(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config: quota-activate: expect bool arg", result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parser", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    OPTIONS, arguments = parser.parse_known_args()
    unittest.main(argv=[__file__, *arguments])
