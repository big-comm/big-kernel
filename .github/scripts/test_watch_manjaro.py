"""Scenarios the Manjaro watcher must get right, against simulated databases.

Run from the repository root: python3 -m unittest discover -s .github/scripts
"""

import io
import os
import re
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

MANJARO = {
    "nvidia-open-dkms": "610.57.04-1",
    "nvidia-dkms": "610.57.04-1",
    "nvidia-580xx-open-dkms": "580.178.04-1",
    "nvidia-580xx-dkms": "580.178.04-1",
    "broadcom-wl-dkms": "6.30.223.271-49.0",
    "nvidia-470xx-dkms": "470.256.02-21",
    "nvidia-390xx-dkms": "390.157-33",
    "virtualbox-host-dkms": "7.2.16-1",
    "zfs-dkms": "2.4.4-1",
    "vhba-module-dkms": "20260313-1",
    "acpi_call-dkms": "1.2.2-3.0",
}


def suffix(module):
    return watch.rebuild_suffix(os.path.join(ROOT, module, "PKGBUILD"), KERNEL)


def up_to_date(manjaro):
    """What BigCommunity has published when every module matches."""
    ours = {"linux-big": KERNEL}
    for module, source in watch.MODULES.items():
        if source:
            ours[module] = f"{watch.without_pkgrel(manjaro[source])}-{REL}{suffix(module)}"
        else:
            ours[module] = f"{watch.pkgbuild_value(os.path.join(ROOT, module, 'PKGBUILD'), 'pkgver')}-{REL}{suffix(module)}"
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
        self.assertEqual([m for _b, _mb, m, _e in builds], ["linux-big"])

    def test_an_older_kernel_published_is_not_enough(self):
        builds = watch.plan(ROOT, databases({"linux-big": "7.2.7-1"}, {}))
        self.assertEqual([m for _b, _mb, m, _e in builds], ["linux-big"])

    def test_a_kernel_ahead_of_testing_is_built_for_testing(self):
        # What the kernel watcher leaves behind: the PKGBUILD moved on,
        # testing still has the previous release. Modules wait for it.
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), {"linux-big": "7.2.7-2"}))
        self.assertEqual(builds, [("testing", "stable", "linux-big", KERNEL)])

    def test_stable_never_gets_a_kernel_by_itself(self):
        builds = watch.plan(ROOT, databases({"linux-big": "7.2.7-2"}, up_to_date(MANJARO)))
        self.assertNotIn("linux-big", [m for b, _mb, m, _e in builds if b == "stable"])

    def test_a_newer_kernel_published_by_hand_is_not_rebuilt_older(self):
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), {"linux-big": "9.9.9-1"}))
        self.assertEqual(builds, [])

    def test_the_kernel_build_is_dispatched_once(self):
        build = ("testing", "testing", "linux-big", KERNEL)
        state = {watch.state_key(*build): "2026-09-27T00:00:00Z"}
        self.assertEqual(watch.pending([build], state), [])

    def test_a_published_kernel_without_modules_builds_all_of_them(self):
        builds = watch.plan(ROOT, databases({"linux-big": KERNEL}, {"linux-big": KERNEL}))
        self.assertEqual(modules(builds, "stable"), sorted(watch.MODULES))
        self.assertEqual(modules(builds, "testing"), sorted(watch.MODULES))

    def test_nothing_is_built_when_everything_matches(self):
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), up_to_date(MANJARO)))
        self.assertEqual(builds, [])

    def test_a_new_driver_in_manjaro_testing_alone_builds_nothing(self):
        # build-package builds against Manjaro stable: a module "for" Manjaro
        # testing's driver would come out with stable's, so none is dispatched.
        newer = dict(MANJARO, **{"nvidia-open-dkms": "615.10.02-1", "nvidia-dkms": "615.10.02-1"})
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), up_to_date(MANJARO), testing_manjaro=newer))
        self.assertEqual(builds, [])

    def test_a_new_driver_in_manjaro_stable_rebuilds_both_branches(self):
        # What 2026-10-07 needed once NVIDIA 615 reaches Manjaro stable: testing
        # too, or community-testing keeps a module older than nvidia-utils.
        newer = dict(MANJARO, **{"nvidia-open-dkms": "615.10.02-1", "nvidia-dkms": "615.10.02-1"})
        both = up_to_date(MANJARO)
        builds = watch.plan(ROOT, databases(both, both, stable_manjaro=newer, testing_manjaro=newer))
        self.assertEqual(modules(builds, "testing"), ["linux-big-nvidia", "linux-big-nvidia-open"])
        self.assertEqual(modules(builds, "stable"), ["linux-big-nvidia", "linux-big-nvidia-open"])
        self.assertEqual({e for _b, _m, _mod, e in builds}, {f"615.10.02-{REL}"})
        self.assertEqual({mb for _b, mb, _mod, _e in builds}, {"stable"})

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
        stable = [(m, e) for b, _mb, m, e in builds if b == "stable"]
        self.assertEqual(stable, [("linux-big-nvidia-open", f"615.10.02-{REL}")])

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
        self.assertEqual([b for b in builds if b[0] == "stable"], [])

    def test_every_module_directory_is_watched(self):
        directories = {d for d in os.listdir(ROOT) if d.startswith("linux-big-") and os.path.isfile(os.path.join(ROOT, d, "PKGBUILD"))}
        self.assertEqual(directories, set(watch.MODULES))

    def test_a_driver_missing_from_manjaro_is_skipped_not_fatal(self):
        partial = {k: v for k, v in MANJARO.items() if k != "nvidia-580xx-dkms"}
        builds = watch.plan(ROOT, databases({"linux-big": KERNEL}, {}, stable_manjaro=partial))
        self.assertNotIn("linux-big-nvidia-580xx", modules(builds, "stable"))


class Rebuild(unittest.TestCase):
    """_rebuild in a module PKGBUILD, as _pkgrel() there reads it."""

    def pkgbuild(self, rebuild_for, rebuild):
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix="PKGBUILD", delete=False)
        handle.write(f"_rebuild_for={rebuild_for}\n_rebuild={rebuild}\npkgrel=$(_pkgrel)\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_rebuild_for_this_kernel_adds_a_suffix(self):
        self.assertEqual(watch.rebuild_suffix(self.pkgbuild("7.2.7-2", "1"), "7.2.7-2"), ".1")

    def test_a_new_kernel_drops_it(self):
        self.assertEqual(watch.rebuild_suffix(self.pkgbuild("7.2.7-2", "1"), "7.2.8-1"), "")

    def test_no_rebuild_no_suffix(self):
        self.assertEqual(watch.rebuild_suffix(self.pkgbuild("", ""), "7.2.7-2"), "")

    def test_the_old_build_is_rebuilt_and_the_rebuild_is_kept(self):
        module = "linux-big-zfs"
        if not suffix(module):
            self.skipTest("no rebuild pending in linux-big-zfs")
        published = up_to_date(MANJARO)
        published[module] = published[module].rsplit(".", 1)[0]
        builds = watch.plan(ROOT, databases(published, {}))
        self.assertEqual([(m, e) for _b, _mb, m, e in builds], [(module, f"2.4.4-{REL}{suffix(module)}")])
        self.assertEqual(watch.plan(ROOT, databases(up_to_date(MANJARO), {})), [])

    def test_the_suffixed_version_is_an_upgrade_until_the_next_kernel(self):
        import subprocess
        def vercmp(a, b):
            return int(subprocess.run(["vercmp", a, b], capture_output=True, text=True).stdout)
        try:
            self.assertEqual(vercmp("2.4.4-7020702.1", "2.4.4-7020702"), 1)
            self.assertEqual(vercmp("2.4.4-7020801", "2.4.4-7020702.1"), 1)
        except FileNotFoundError:
            self.skipTest("vercmp (pacman) not installed")


class Versions(unittest.TestCase):
    def test_kernel_versions_compare_as_numbers(self):
        self.assertGreater(watch.version_key("7.2.10-1"), watch.version_key("7.2.9-3"))
        self.assertGreater(watch.version_key("7.2.8-2"), watch.version_key("7.2.8-1"))
        self.assertGreater(watch.version_key("7.3-1"), watch.version_key("7.2.99-1"))


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


class MovedToStable(unittest.TestCase):
    """Testing is seen as a testing user sees it: its packages over stable's."""

    def setUp(self):
        watch.log = lambda _message: None

    def test_moving_everything_to_stable_rebuilds_nothing(self):
        # Repo-Management moves the files: testing is left without linux-big.
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), {}))
        self.assertEqual(builds, [])

    def test_a_newer_driver_only_in_manjaro_testing_builds_nothing(self):
        newer = dict(MANJARO, **{"nvidia-open-dkms": "615.71.09-1"})
        builds = watch.plan(ROOT, databases(up_to_date(MANJARO), {}, testing_manjaro=newer))
        self.assertEqual(builds, [])


class StableBehindMain(unittest.TestCase):
    """Stable keeps its kernel while main moves on: its modules still follow
    Manjaro stable, built from the commit that described that kernel."""

    def setUp(self):
        import shutil
        import subprocess
        import tempfile
        watch.log = lambda _message: None
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        for entry in ["linux-big", *watch.MODULES]:
            os.makedirs(os.path.join(self.repo, entry))
            shutil.copy(os.path.join(ROOT, entry, "PKGBUILD"), os.path.join(self.repo, entry, "PKGBUILD"))

        def git(*args):
            return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True, check=True).stdout.strip()

        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"linux-big {KERNEL}")
        self.stable_commit = git("rev-parse", "HEAD")
        kernel = os.path.join(self.repo, "linux-big", "PKGBUILD")
        text = open(kernel).read()
        text = re.sub(r"^pkgver=.*$", "pkgver=7.2.99", text, count=1, flags=re.M)
        text = re.sub(r"^pkgrel=.*$", "pkgrel=1", text, count=1, flags=re.M)
        open(kernel, "w").write(text)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "linux-big 7.2.99-1")

    def test_a_new_driver_in_stable_is_built_from_stables_commit(self):
        newer = dict(MANJARO, **{"nvidia-open-dkms": "615.71.09-1"})
        refs = {}
        builds = watch.plan(self.repo, databases(up_to_date(MANJARO), up_to_date(MANJARO), stable_manjaro=newer), refs=refs)
        stable = [b for b in builds if b[0] == "stable"]
        # The stable kernel's pkgrel, not main's: built against the kernel stable has.
        self.assertEqual(stable, [("stable", "stable", "linux-big-nvidia-open", f"615.71.09-{REL}")])
        self.assertEqual(refs, {"stable": self.stable_commit})
        # Testing follows main: it gets the new kernel.
        self.assertIn(("testing", "stable", "linux-big", "7.2.99-1"), builds)

    def test_nothing_to_do_for_stable_when_its_modules_match(self):
        refs = {}
        builds = watch.plan(self.repo, databases(up_to_date(MANJARO), up_to_date(MANJARO)), refs=refs)
        self.assertEqual([b for b in builds if b[0] == "stable"], [])

    def test_a_stable_kernel_no_commit_describes_is_left_alone(self):
        refs = {}
        builds = watch.plan(self.repo, databases(up_to_date(MANJARO), {}), refs=refs, find=lambda root, k: None)
        self.assertEqual([b for b in builds if b[0] == "stable"], [])
        self.assertEqual(refs, {})

    def test_the_commit_is_found_by_the_kernel_it_describes(self):
        self.assertEqual(watch.find_commit(self.repo, KERNEL), self.stable_commit)
        self.assertIsNone(watch.find_commit(self.repo, "6.1.0-1"))


class StableDispatch(unittest.TestCase):
    """Stable builds that need an older commit run from linux-big-stable."""

    def test_the_branch_is_pointed_once_and_used_by_every_stable_build(self):
        import tempfile
        sent, pointed = [], []
        state_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        state_file.write("{}")
        state_file.close()
        self.addCleanup(os.unlink, state_file.name)
        builds = [
            ("stable", "stable", "linux-big-nvidia", "615.71.09-7020801"),
            ("stable", "stable", "linux-big-nvidia-open", "615.71.09-7020801"),
            ("testing", "testing", "linux-big", "7.2.99-1"),
        ]

        def fake_plan(root, fetch, refs=None, **_kw):
            refs["stable"] = "abc123def456"
            return builds

        saved = (watch.plan, watch.building, watch.dispatch, watch.point_branch, watch.log, sys.argv)
        watch.plan, watch.building = fake_plan, (lambda token: set())
        watch.dispatch = lambda token, body: sent.append((body["event_type"], body["client_payload"]["branch"])) or 204
        watch.point_branch = lambda root, name, commit: pointed.append((name, commit))
        watch.log = lambda _message: None
        sys.argv = ["watch_manjaro.py", "--state", state_file.name]
        os.environ["DISPATCH_TOKEN"] = "x"
        github_output = os.environ.pop("GITHUB_OUTPUT", None)
        try:
            watch.main()
        finally:
            watch.plan, watch.building, watch.dispatch, watch.point_branch, watch.log, sys.argv = saved
            del os.environ["DISPATCH_TOKEN"]
            if github_output is not None:
                os.environ["GITHUB_OUTPUT"] = github_output
        self.assertEqual(pointed, [(watch.STABLE_REF, "abc123def456")])
        self.assertEqual(sent, [
            ("linux-big-nvidia", watch.STABLE_REF),
            ("linux-big-nvidia-open", watch.STABLE_REF),
            ("linux-big", "main"),
        ])


class PointBranch(unittest.TestCase):
    def test_only_a_full_commit_hash_is_pushed(self):
        for commit in ("abc123", "HEAD", "main", "--force", "0" * 39 + "; rm -rf /"):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                watch.point_branch(".", watch.STABLE_REF, commit)

    def test_a_pkgbuild_without_a_version_is_reported_by_name(self):
        with self.assertRaises(watch.PkgbuildError) as raised:
            watch.kernel_version("pkgname=linux-big\n")
        self.assertIn("linux-big/PKGBUILD", str(raised.exception))


class InFlight(unittest.TestCase):
    """A package build-package is already building is not dispatched again."""

    def run_main(self, active, builds):
        import tempfile
        sent = []
        state_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        state_file.write("{}")
        state_file.close()
        self.addCleanup(os.unlink, state_file.name)
        saved = (watch.plan, watch.building, watch.dispatch, sys.argv, os.environ.get("DISPATCH_TOKEN"))
        watch.plan = lambda root, fetch, **_kw: builds
        watch.building = lambda token: active
        watch.dispatch = lambda token, body: sent.append(body["event_type"]) or 204
        sys.argv = ["watch_manjaro.py", "--state", state_file.name]
        os.environ["DISPATCH_TOKEN"] = "x"
        github_output = os.environ.pop("GITHUB_OUTPUT", None)
        if github_output is not None:
            self.addCleanup(os.environ.__setitem__, "GITHUB_OUTPUT", github_output)
        try:
            watch.main()
        finally:
            watch.plan, watch.building, watch.dispatch, sys.argv = saved[:4]
            if saved[4] is None:
                del os.environ["DISPATCH_TOKEN"]
            else:
                os.environ["DISPATCH_TOKEN"] = saved[4]
        return sent

    def setUp(self):
        watch.log = lambda _message: None

    def test_a_kernel_being_built_is_not_dispatched_again(self):
        # What happened with 7.2.8: the dispatch state was lost on a re-run
        # and the next run sent a second four-hour build.
        builds = [("testing", "testing", "linux-big", "7.2.8-1")]
        self.assertEqual(self.run_main({"linux-big"}, builds), [])

    def test_only_the_packages_in_flight_are_skipped(self):
        builds = [
            ("testing", "testing", "linux-big-nvidia", "615.71.09-7020801"),
            ("testing", "testing", "linux-big-zfs", "2.4.4-7020801"),
        ]
        self.assertEqual(self.run_main({"linux-big-nvidia"}, builds), ["linux-big-zfs"])

    def test_nothing_in_flight_dispatches_everything(self):
        builds = [("testing", "testing", "linux-big", "7.2.8-1")]
        self.assertEqual(self.run_main(set(), builds), ["linux-big"])


class WaitForKernel(unittest.TestCase):
    """The modules follow the kernel as soon as it is published."""

    def run_wait(self, published, in_flight, **limits):
        clock = {"t": 0}
        states = iter(published)
        last = {"v": None}

        def get_published():
            last["v"] = next(states, last["v"])
            return last["v"]

        def sleep(seconds):
            clock["t"] += seconds

        return watch.wait_for_kernel("7.2.8-1", get_published, lambda: in_flight(clock["t"]),
                                     sleep=sleep, now=lambda: clock["t"], **limits)

    def test_published_after_a_while(self):
        result = self.run_wait(["7.2.7-2"] * 40 + ["7.2.8-1"], lambda t: True)
        self.assertEqual(result, "published")

    def test_a_failed_build_is_reported_not_waited_for(self):
        # Nothing building any more and the kernel never appeared.
        result = self.run_wait(["7.2.7-2"] * 1000, lambda t: t < 3600)
        self.assertEqual(result, "failed")

    def test_the_dispatch_gets_time_to_show_up_as_a_run(self):
        # Right after the dispatch build-package has no run yet: not a failure.
        result = self.run_wait(["7.2.7-2"] * 3 + ["7.2.8-1"], lambda t: False)
        self.assertEqual(result, "published")

    def test_gives_up_after_the_limit(self):
        result = self.run_wait(["7.2.7-2"] * 10000, lambda t: True, minutes=60)
        self.assertEqual(result, "timeout")


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
