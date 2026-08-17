from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CatalogBrowserTests(unittest.TestCase):
    def test_frontend_regression_suite(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the dependency-free frontend tests.")
        completed = subprocess.run(
            [node, "--test", "tests/catalog_browser.test.js"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"Frontend tests failed:\n{completed.stdout}\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
