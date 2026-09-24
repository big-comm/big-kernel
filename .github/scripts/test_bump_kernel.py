"""Scenarios the kernel watcher must get right, on a miniature kernel.

Run from the repository root: python3 -m unittest discover -s .github/scripts
"""

import difflib
import hashlib
import io
import json
import lzma
import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import bump_kernel as bump  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_PKGBUILD = open(os.path.join(ROOT, "linux-big", "PKGBUILD"), encoding="utf-8").read()


def releases(*entries):
    return {"releases": [{"moniker": m, "version": v, "iseol": eol} for m, v, eol in entries]}


KERNEL_ORG = releases(
    ("mainline", "7.3-rc4", False),
    ("stable", "7.2.9", False),
    ("stable", "7.1.13", True),
    ("longterm", "6.18.53", False),
)


class Series(unittest.TestCase):
    def test_the_newest_release_of_the_series(self):
        self.assertEqual(bump.latest_in_series(KERNEL_ORG, "7.2"), ("7.2.9", False))

    def test_a_series_marked_end_of_life(self):
        self.assertEqual(bump.latest_in_series(KERNEL_ORG, "7.1"), ("7.1.13", True))

    def test_a_series_no_longer_listed_is_end_of_life(self):
        self.assertEqual(bump.latest_in_series(KERNEL_ORG, "7.0"), (None, True))

    def test_7_2_is_not_7_20(self):
        self.assertEqual(bump.latest_in_series(releases(("stable", "7.20.1", False)), "7.2"), (None, True))

    def test_a_new_series_before_its_first_stable_release(self):
        listed = releases(("mainline", "7.3", False), ("stable", "7.2.9", False))
        self.assertEqual(bump.latest_in_series(listed, "7.3"), ("7.3", False))

    def test_release_candidates_do_not_count(self):
        self.assertEqual(bump.latest_in_series(KERNEL_ORG, "7.3"), (None, True))

    def test_versions_compare_as_numbers(self):
        self.assertGreater(bump.version_key("7.2.10"), bump.version_key("7.2.9"))


class RealPkgbuild(unittest.TestCase):
    """The edits, on the PKGBUILD the watcher will actually change."""

    def test_the_stable_patch_is_the_second_source(self):
        self.assertEqual(bump.patch_index(REAL_PKGBUILD), 1)

    def test_every_source_has_a_checksum(self):
        self.assertEqual(len(bump.source_entries(REAL_PKGBUILD)), len(bump.checksums(REAL_PKGBUILD)))

    def test_bump_changes_only_version_release_and_patch_checksum(self):
        new = bump.bump(REAL_PKGBUILD, "7.2.9", "a" * 64)
        self.assertEqual(bump.pkgbuild_value(new, "pkgver"), "7.2.9")
        self.assertEqual(bump.pkgbuild_value(new, "pkgrel"), "1")
        old_sums, new_sums = bump.checksums(REAL_PKGBUILD), bump.checksums(new)
        self.assertEqual(new_sums[1], "a" * 64)
        self.assertEqual(new_sums[:1] + new_sums[2:], old_sums[:1] + old_sums[2:])
        self.assertEqual(bump.source_entries(new), bump.source_entries(REAL_PKGBUILD))
        changed = [line for line in new.splitlines() if line not in REAL_PKGBUILD.splitlines()]
        self.assertEqual(len(changed), 3, changed)

    def test_drop_removes_the_patch_and_its_own_checksum(self):
        entries, sums = bump.source_entries(REAL_PKGBUILD), bump.checksums(REAL_PKGBUILD)
        name = "0000-iwlwifi-fix.patch"
        index = entries.index(name)
        new = bump.drop_patch(REAL_PKGBUILD, name)
        self.assertEqual(bump.source_entries(new), entries[:index] + entries[index + 1 :])
        self.assertEqual(bump.checksums(new), sums[:index] + sums[index + 1 :])
        self.assertIn("# BigCommunity: Intel display and Xe fixes", new)


class Failure(unittest.TestCase):
    def test_names_the_patch_that_failed(self):
        output = "\n".join(
            [
                "Applying patch 0001-a.patch...",
                "patching file drivers/a.c",
                "Applying patch 0002-b.patch...",
                "patching file drivers/b.c",
                "Hunk #1 FAILED at 10.",
                "1 out of 1 hunk FAILED -- saving rejects to file drivers/b.c.rej",
                "==> ERROR: A failure occurred in prepare().",
            ]
        )
        reasons = bump.failure_reasons(output)
        self.assertEqual(reasons[0], "Applying patch 0002-b.patch...")
        self.assertNotIn("Applying patch 0001-a.patch...", reasons)
        self.assertIn("Hunk #1 FAILED at 10.", reasons)

    def test_names_a_lost_option(self):
        reasons = bump.failure_reasons("==> ERROR: These options did not survive the configuration: CONFIG_SCHED_BORE=y")
        self.assertIn("CONFIG_SCHED_BORE=y", reasons[0])


PKGBUILD = """\
_basekernel=7.2
pkgbase=linux-big
pkgver=7.2.6
pkgrel=3
source=(https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-${_basekernel}.tar.xz
        https://cdn.kernel.org/pub/linux/kernel/v7.x/patch-${pkgver}.xz
        config
        # ours
%s
)
sha256sums=(%s)
"""

A = ["old a1\n", "old a2\n", "old a3\n"]
A_OURS = ["ours a1\n", "ours a2\n", "ours a3\n"]
B, B_FIXED = ["old b\n"], ["fixed b\n"]


def diff(path, before, after):
    """A real unified diff, three lines of context, as git writes them."""
    return "".join(difflib.unified_diff(before, after, f"a/{path}", f"b/{path}"))


class Main(unittest.TestCase):
    """A whole run, on a kernel of two files, without makepkg.

    0002 is written on top of 0001: every line of its context is one 0001
    changed, so it fits the tree only once 0001 is applied. 0003 is what
    7.2.9 fixed. Only by applying the series in order, as prepare() does,
    does the watcher get past 0002 and find 0003 already in the kernel.
    """

    def setUp(self):
        bump.log = lambda _message: None
        self.work = tempfile.TemporaryDirectory()
        base = self.work.name
        self.root, self.cache = os.path.join(base, "repo"), os.path.join(base, "cache")
        self.kernel = os.path.join(self.root, "linux-big")
        os.makedirs(self.kernel)
        os.makedirs(self.cache)
        with tarfile.open(os.path.join(self.cache, "linux-7.2.tar.xz"), "w:xz") as archive:
            for name, lines in (("a.c", A), ("b.c", B)):
                data = "".join(lines).encode()
                info = tarfile.TarInfo(f"linux-7.2/{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with lzma.open(os.path.join(self.cache, "patch-7.2.9.xz"), "wb") as out:
            out.write(diff("b.c", B, B_FIXED).encode())
        self.patch_sha = hashlib.sha256(open(os.path.join(self.cache, "patch-7.2.9.xz"), "rb").read()).hexdigest()
        self.releases = os.path.join(base, "releases.json")
        self.result = os.path.join(base, "result.json")

    def tearDown(self):
        self.work.cleanup()

    def series(self, patches):
        for name, text in patches:
            open(os.path.join(self.kernel, name), "w").write(text)
        self.sums = ["1" * 64, "2" * 64, "3" * 64] + [c * 64 for c in "456789"[: len(patches)]]
        entries = "\n".join(f"        {name}" for name, _text in patches)
        sums = "\n            ".join(f"'{s}'" for s in self.sums)
        open(os.path.join(self.kernel, "PKGBUILD"), "w").write(PKGBUILD % (entries, sums))

    def run_watcher(self, listed):
        json.dump(listed, open(self.releases, "w"))
        argv = ["bump_kernel.py", "--root", self.root, "--cache", self.cache, "--releases", self.releases,
                "--result", self.result, "--no-prepare"]
        old, sys.argv = sys.argv, argv
        try:
            self.assertEqual(bump.main(), 0)
        finally:
            sys.argv = old
        return json.load(open(self.result))

    def pkgbuild(self):
        return open(os.path.join(self.kernel, "PKGBUILD")).read()

    def test_a_patch_already_in_the_release_is_found_behind_a_stacked_one(self):
        self.series([
            ("0001-ours.patch", diff("a.c", A, A_OURS)),
            ("0002-on-top.patch", diff("a.c", A_OURS, A_OURS + ["more a\n"])),
            ("0003-merged.patch", diff("b.c", B, B_FIXED)),
        ])
        result = self.run_watcher(KERNEL_ORG)
        self.assertEqual((result["status"], result["from"], result["to"]), ("bumped", "7.2.6", "7.2.9"))
        self.assertEqual(result["dropped"], ["0003-merged.patch"])
        text = self.pkgbuild()
        self.assertEqual(bump.pkgbuild_value(text, "pkgver"), "7.2.9")
        self.assertEqual(bump.pkgbuild_value(text, "pkgrel"), "1")
        self.assertEqual(bump.source_entries(text)[3:], ["0001-ours.patch", "0002-on-top.patch"])
        self.assertEqual(bump.checksums(text), [self.sums[0], self.patch_sha, self.sums[2], self.sums[3], self.sums[4]])
        self.assertFalse(os.path.exists(os.path.join(self.kernel, "0003-merged.patch")))

    def test_the_stacked_patch_fits_only_on_top_of_its_base(self):
        # What makes the test above meaningful: alone, 0002 fits neither way.
        with tempfile.TemporaryDirectory() as workdir:
            tree = bump.prepare_tree(workdir, os.path.join(self.cache, "linux-7.2.tar.xz"),
                                     os.path.join(self.cache, "patch-7.2.9.xz"))
            patch = os.path.join(workdir, "0002.patch")
            open(patch, "w").write(diff("a.c", A_OURS, A_OURS + ["more a\n"]))
            self.assertFalse(bump.applies(tree, patch))
            self.assertFalse(bump.applies(tree, patch, reverse=True))

    def test_a_patch_that_fits_neither_way_stops_the_search(self):
        # 0001 no longer fits: prepare() is left to fail on it and name it,
        # and nothing behind it is dropped on a guess.
        self.series([
            ("0001-broken.patch", diff("a.c", ["other\n"] * 3, ["changed\n"] * 3)),
            ("0002-merged.patch", diff("b.c", B, B_FIXED)),
        ])
        result = self.run_watcher(KERNEL_ORG)
        self.assertEqual(result["dropped"], [])
        self.assertEqual(bump.source_entries(self.pkgbuild())[3:], ["0001-broken.patch", "0002-merged.patch"])

    def test_nothing_changes_when_current(self):
        self.series([("0001-ours.patch", diff("a.c", A, A_OURS))])
        before = self.pkgbuild()
        self.assertEqual(self.run_watcher(releases(("stable", "7.2.6", False)))["status"], "current")
        self.assertEqual(self.pkgbuild(), before)

    def test_end_of_life_is_reported_not_acted_on(self):
        self.series([("0001-ours.patch", diff("a.c", A, A_OURS))])
        before = self.pkgbuild()
        self.assertEqual(self.run_watcher(releases(("stable", "7.2.9", True)))["status"], "eol")
        self.assertEqual(self.pkgbuild(), before)


if __name__ == "__main__":
    unittest.main()
