"""The engine stays generic: a small import allowlist, and no host's private words.

A host that uses this package checks its own pinned copy against its private terms by pointing
DOC_INTAKE_FORBIDDEN_TERMS at a file of terms, one per line. The file is read here and never
printed: a failure reports file names and counts only. Without it, that check is skipped.
"""
import ast
import os
import re
import sys
import unittest

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "doc_intake")
OPTIONAL = {"zxingcpp", "PIL"}  # imported inside functions, only when installed


def files():
    return [os.path.join(PKG, n) for n in sorted(os.listdir(PKG)) if n.endswith(".py")]


class Generic(unittest.TestCase):
    def test_imports_are_stdlib_own_or_optional(self):
        bad = {}
        for p in files():
            for node in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module.split(".")[0]]
                for n in names:
                    if n not in sys.stdlib_module_names and n not in OPTIONAL and n != "doc_intake":
                        bad.setdefault(os.path.basename(p), set()).add(n)
        self.assertEqual(bad, {})

    def test_no_forbidden_terms(self):
        path = os.environ.get("DOC_INTAKE_FORBIDDEN_TERMS")
        if not path:
            self.skipTest("DOC_INTAKE_FORBIDDEN_TERMS not set (a host runs this against its pinned copy)")
        with open(path, encoding="utf-8") as f:
            terms = [t.strip() for t in f if t.strip() and not t.startswith("#")]
        self.assertTrue(terms, "the terms file is empty")
        hits = {}
        for p in files() + [os.path.join(os.path.dirname(PKG), "README.md")]:
            if not os.path.exists(p):
                continue
            text = open(p, encoding="utf-8").read()
            n = sum(bool(re.search(re.escape(t), text, re.I)) for t in terms)
            if n:
                hits[os.path.basename(p)] = n
        self.assertEqual(hits, {}, "files holding forbidden terms (counts only)")


if __name__ == "__main__":
    unittest.main()
