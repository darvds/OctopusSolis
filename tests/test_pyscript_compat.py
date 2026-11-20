"""Pyscript's interpreter doesn't support every Python construct: keep the app to what it runs."""
import ast
import unittest

from tests.harness import APP_PATH

# Pyscript raises NotImplementedError for these when the code runs.
UNSUPPORTED = (ast.GeneratorExp, ast.Yield, ast.YieldFrom)


class PyscriptCompatTests(unittest.TestCase):
    def test_no_unsupported_syntax(self):
        source = APP_PATH.read_text()
        found = [f"line {n.lineno}: {type(n).__name__}" for n in ast.walk(ast.parse(source))
                 if isinstance(n, UNSUPPORTED)]
        self.assertEqual(found, [], "pyscript can't run these; use a list comprehension or a loop")


class VersionTests(unittest.TestCase):
    def test_changelog_has_the_current_version(self):
        import re
        from pathlib import Path
        version = re.search(r'^VERSION = "([^"]+)"', APP_PATH.read_text(), re.M).group(1)
        changelog = (Path(APP_PATH).parents[2] / "CHANGELOG.md").read_text()
        self.assertIn(f"## {version}", changelog, "add a CHANGELOG.md entry for the new VERSION")
