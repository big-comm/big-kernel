"""Scenarios the Manjaro watcher must get right, against simulated databases.

Run from the repository root: python3 -m unittest discover -s .github/scripts
"""

import io
import os
import sys
import tarfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import watch_manjaro as watch  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KERNEL = "{}-{}".format(
    watch.pkgbuild_value(os.path.join(ROOT, "linux-big", "PKGBUILD"), "pkgver"),
    watch.pkgbuild_value(os.path.join(ROOT, "linux-big", "PKGBUILD"), "pkgrel"),
)
REL = watch.kernel_rel(*KERNEL.split("-"))
BBSWITCH = watch.pkgbuild_value(os.path.join(ROOT, "linux-big-bbswitch", "PKGBUILD"), "pkgver")

MANJARO = {
    "nvidia-open-dkms": "610.57.04-1",
    "nvidia-dkms": "610.57.04-1",
    "nvidia-580xx-open-dkms": "580.178.04-1",
    "nvidia-580xx-dkms": "580.178.04-1",
    "broadcom-wl-dkms": "6.30.223.271-49.0",
}


def up_to_date(manjaro):
    """What BigCommunity has published when every module matches."""
    ours = {"linux-big": KERNEL, "linux-big-bbswitch": f"{BBSWITCH}-{REL}"}
    for module, source in watch.MODULES.items():
        if source:
            ours[module] = f"{watch.without_pkgrel(manjaro[source])}-{REL}"
    return ours


def databases(stable_ours, testing_ours, stable_manjaro=MANJARO, testing_manjaro=MANJARO, **extra):
    """Every repository the watcher reads; the ones not given are empty."""
    by_url = {
        watch.BRANCHES["stable"][1]: stable_ours,
        watch.BRANCHES["testing"][1]: testing_ours,
        watch.MANJARO_DB.format(branch="stable"): stable_manjaro,
        watch.MANJARO_DB.format(branch="testing"): testing_manjaro,
    }
    for name, db in extra.items():
        by_url[watch.BIGLINUX_DB.format(branch=name.replace("_", "-"))] = db
    known = {url for _m, _o, search in watch.BRANCHES.values() for url in search} | set(by_url)

    def fetch(url):
        if url not in known:
            raise AssertionError(f"unexpected repository {url}")
        return by_url.get(url, {})

    return fetch


def modules(builds, branch=None):
    return sorted(module for b, _m, module, _e in builds if branch in (None, b))


class Plan(unittest.TestCase):
    def setUp(self):
        watch.log = lambda _message: None

    def test_modules_wait_for_the_kernel(self):
        builds = watch.plan(ROOT, databases({}, {}))
        self.assertEqual(builds, [])

    def test_an_older_kernel_published_is_not_enough(self):
        builds = watch.plan(ROOT, databases({"linux-big": "7.2.7-1"}, {}))
        self.assertEqual(builds, [])

    def test_a_published_kernel_without_modules_builds_all_of_them(self):
        builds = watch.plan(ROOT, databases({"linux-big": KERNEL}, {}))
        self.assertEqual(modules(builds, "stable"), sorted(watch.MODULES))
        self.assertEqual(modules(builds, "testing"), [])

    def test_nothing_is_built_when_everything_matches(self):
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), up_to_date(MANJARO)))
        self.assertEqual(builds, [])

    def test_a_new_driver_in_manjaro_testing_rebuilds_only_its_modules_there(self):
        newer = dict(MANJARO, **{"nvidia-open-dkms": "615.10.02-1", "nvidia-dkms": "615.10.02-1"})
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), up_to_date(MANJARO), testing_manjaro=newer))
        self.assertEqual(modules(builds, "testing"), ["linux-big-nvidia", "linux-big-nvidia-open"])
        self.assertEqual(modules(builds, "stable"), [])
        expected = {e for _b, _m, _mod, e in builds}
        self.assertEqual(expected, {f"615.10.02-{REL}"})

    def test_a_new_kernel_rebuilds_every_module(self):
        old_rel = watch.kernel_rel("7.2.7", "1")
        published = {k: v.replace(f"-{REL}", f"-{old_rel}") for k, v in up_to_date(MANJARO).items()}
        published["linux-big"] = KERNEL
        builds = watch.plan(ROOT, databases(published, {}))
        self.assertEqual(modules(builds, "stable"), sorted(watch.MODULES))

    def test_a_driver_biglinux_publishes_first_wins_as_it_does_for_pacman(self):
        # biglinux-update-stable comes before Manjaro's extra in pacman.conf.
        biglinux = {"nvidia-open-dkms": "615.10.02-1"}
        builds = watch.plan(
            ROOT, databases(up_to_date(MANJARO), {}, update_stable=biglinux)
        )
        self.assertEqual([(m, e) for _b, _mb, m, e in builds], [("linux-big-nvidia-open", f"615.10.02-{REL}")])

    def test_a_driver_only_in_biglinux_stable_is_found(self):
        partial = {k: v for k, v in MANJARO.items() if k != "broadcom-wl-dkms"}
        builds = watch.plan(
            ROOT,
            databases({"linux-big": KERNEL}, {}, stable_manjaro=partial, stable={"broadcom-wl-dkms": "6.30.223.271-50"}),
        )
        found = {m: e for _b, _mb, m, e in builds}
        self.assertEqual(found["linux-big-broadcom-wl"], f"6.30.223.271-{REL}")

    def test_manjaro_still_wins_over_biglinux_stable_which_comes_after_it(self):
        builds = watch.plan(
            ROOT, databases(up_to_date(MANJARO), {}, stable={"nvidia-open-dkms": "999.0-1"})
        )
        self.assertEqual(builds, [])

    def test_a_driver_missing_from_manjaro_is_skipped_not_fatal(self):
        partial = {k: v for k, v in MANJARO.items() if k != "nvidia-580xx-dkms"}
        builds = watch.plan(ROOT, databases({"linux-big": KERNEL}, {}, stable_manjaro=partial))
        self.assertNotIn("linux-big-nvidia-580xx", modules(builds))


class Rel(unittest.TestCase):
    def test_kernel_releases_order_as_pkgrel(self):
        order = [("7.2.7", "1"), ("7.2.7", "2"), ("7.2.10", "1"), ("7.3", "1"), ("7.3.0", "2"), ("8.0.1", "1")]
        values = [int(watch.kernel_rel(v, r)) for v, r in order]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), len(values))


class Dedupe(unittest.TestCase):
    build = ("testing", "testing", "linux-big-nvidia-open", "615.10.02-7020702")

    def test_a_version_is_dispatched_once(self):
        state = {watch.state_key(*self.build): "2026-09-24T00:00:00Z"}
        self.assertEqual(watch.pending([self.build], state), [])

    def test_force_dispatches_it_again(self):
        state = {watch.state_key(*self.build): "2026-09-24T00:00:00Z"}
        self.assertEqual(watch.pending([self.build], state, force=True), [self.build])

    def test_a_newer_version_is_dispatched(self):
        state = {watch.state_key(*self.build): "2026-09-24T00:00:00Z"}
        newer = self.build[:3] + ("615.20.01-7020702",)
        self.assertEqual(watch.pending([newer], state), [newer])


class Payload(unittest.TestCase):
    def test_matches_what_build_package_reads(self):
        body = watch.payload("testing", "testing", "linux-big-nvidia-open")
        self.assertEqual(body["event_type"], "linux-big-nvidia-open")
        data = body["client_payload"]
        self.assertEqual(data["pkgbuild_dir"], "linux-big-nvidia-open")
        self.assertEqual(data["url"], "https://github.com/big-comm/big-kernel")
        self.assertEqual((data["branch_type"], data["manjaro_branch"], data["new_branch"]), ("testing", "testing", "main"))
        self.assertNotIn("new_branch", watch.payload("stable", "stable", "x")["client_payload"])


class Database(unittest.TestCase):
    def test_reads_a_pacman_database(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, version in (("linux-big", "7.2.7-2"), ("nvidia-utils", "610.57.04-1")):
                desc = f"%FILENAME%\n{name}.pkg.tar.zst\n\n%NAME%\n{name}\n\n%VERSION%\n{version}\n\n".encode()
                info = tarfile.TarInfo(f"{name}-{version}/desc")
                info.size = len(desc)
                archive.addfile(info, io.BytesIO(desc))
        self.assertEqual(
            watch.read_database(buffer.getvalue()), {"linux-big": "7.2.7-2", "nvidia-utils": "610.57.04-1"}
        )


if __name__ == "__main__":
    unittest.main()
