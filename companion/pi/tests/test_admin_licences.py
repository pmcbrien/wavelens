"""Admin page: the open-source licences page and its licence-file downloads.
Standard library only:
    python3 -m unittest companion/pi/tests/test_admin_licences.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = pathlib.Path(HERE).parents[2]
sys.path.insert(0, os.path.join(HERE, ".."))


def load_admin():
    spec = importlib.util.spec_from_file_location("wavelens_admin_for_licences_test", os.path.join(HERE, "..", "wavelens_admin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = load_admin()


class Licences(unittest.TestCase):
    def test_every_component_is_in_the_repo_with_its_licence(self):
        # the page promises the source is in the repository: each folder and licence file must exist
        for key, _name, _what, _lic, folder, lic, _up in A.OSS:
            self.assertTrue((REPO / folder).is_dir(), f"{key}: {folder} missing")
            if lic:
                archive = lic.partition(":")[0]     # "x.tar.gz:x/LICENSE": the licence inside a release tarball
                self.assertTrue((REPO / folder / archive).is_file(), f"{key}: {folder}/{archive} missing")
                A.source_dir = lambda: REPO
                self.assertTrue(A.oss_licence_text(key), f"{key}: licence text not readable")

    def test_licence_text_comes_from_the_checkout(self):
        A.source_dir = lambda: REPO
        self.assertIn("GNU GENERAL PUBLIC LICENSE", A.oss_licence_text("apco25"))

    def test_only_listed_components_are_served(self):
        A.source_dir = lambda: REPO
        for bad in ("../../etc/passwd", "apco25/../../../etc/passwd", "", "nope", "bluezero-0.9.1/LICENSE"):
            self.assertIsNone(A.oss_licence_text(bad), bad)

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            A.source_dir = lambda: pathlib.Path(d)
            self.assertIsNone(A.oss_licence_text("apco25"))

    def test_page_renders(self):
        out = {}

        class Stub:
            def render(self, sess, title, body, active):
                out["title"], out["body"] = title, body

        A.Handler.page_licences(Stub(), {"csrf": "x"}) if hasattr(A, "Handler") else None
        if not out:
            # the handler class has another name: find the one that defines page_licences
            cls = next(v for v in vars(A).values() if isinstance(v, type) and hasattr(v, "page_licences"))
            cls.page_licences(Stub(), {"csrf": "x"})
        self.assertEqual(out["title"], "Open-source licences")
        for word in ("APCO25", "dsd-fme", "mbelib", "Written offer", A.PUBLIC_REPO):
            self.assertIn(word, out["body"])

    def test_footer_links_the_page(self):
        self.assertIn('href="/licences"', A.page("x", "<p>y</p>"))


if __name__ == "__main__":
    unittest.main()
