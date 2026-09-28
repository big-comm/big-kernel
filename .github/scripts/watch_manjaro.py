#!/usr/bin/env python3
"""Keep the linux-big kernel modules in step with Manjaro and with linux-big.

The module packages (linux-big-nvidia-open, linux-big-broadcom-wl, ...) each
depend on one exact linux-big and on one exact driver version. They go stale
in two ways: a new linux-big is published, or Manjaro publishes a new driver.
Either way the right version is computable, and this script compares it with
what BigCommunity has published, branch by branch, and dispatches a build of
every module that is behind. The driver version is the one users get: the
first of their repositories that has it -- BigLinux, Manjaro, BigCommunity --
in their pacman.conf order, since that is how pacman chooses.

Nothing is edited: the module PKGBUILDs read the driver version from pacman
and the kernel version from ../linux-big/PKGBUILD when they build, so a build
dispatched from the current repository always produces the expected version.

A module is only dispatched on a branch once linux-big itself is published
there, since the build needs linux-big-headers of that exact version.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

REPOSITORY_URL = "https://github.com/big-comm/big-kernel"
USER_AGENT = f"big-kernel-watch/1 (+{REPOSITORY_URL})"
WORKFLOW_REPOSITORY = "big-comm/build-package"

# Module directory -> the Manjaro package its driver version follows, or None
# when the version is fixed in the PKGBUILD itself.
MODULES = {
    "linux-big-nvidia-open": "nvidia-open-dkms",
    "linux-big-nvidia": "nvidia-dkms",
    "linux-big-nvidia-580xx-open": "nvidia-580xx-open-dkms",
    "linux-big-nvidia-580xx": "nvidia-580xx-dkms",
    "linux-big-nvidia-470xx": "nvidia-470xx-dkms",
    "linux-big-nvidia-390xx": "nvidia-390xx-dkms",
    "linux-big-broadcom-wl": "broadcom-wl-dkms",
    "linux-big-virtualbox-host-modules": "virtualbox-host-dkms",
    "linux-big-zfs": "zfs-dkms",
    "linux-big-vhba-module": "vhba-module-dkms",
    "linux-big-acpi_call": "acpi_call-dkms",
    "linux-big-bbswitch": None,
    "linux-big-r8168": None,
    "linux-big-rtl8723bu": None,
    "linux-big-tp_smapi": None,
}

MANJARO_DB = "https://mirrors.manjaro.org/repo/{branch}/extra/x86_64/extra.db"
COMMUNITY_DB = "https://repo.communitybig.org/{branch}/x86_64/community-{branch}.db"
BIGLINUX_DB = "https://repo.biglinux.com.br/{branch}/x86_64/biglinux-{branch}.db"

# BigCommunity branch -> the Manjaro branch its builds run against, the
# database modules are published to, and the repositories a user of that
# branch has, in their pacman.conf order. pacman takes a package from the
# first repository that has it, so that order decides which driver version
# users get: a driver BigLinux publishes ahead of Manjaro's wins.
BRANCHES = {
    "stable": (
        "stable",
        COMMUNITY_DB.format(branch="stable"),
        [
            BIGLINUX_DB.format(branch="update-stable"),
            MANJARO_DB.format(branch="stable"),
            COMMUNITY_DB.format(branch="stable"),
            COMMUNITY_DB.format(branch="extra"),
            BIGLINUX_DB.format(branch="stable"),
        ],
    ),
    "testing": (
        "testing",
        COMMUNITY_DB.format(branch="testing"),
        [
            BIGLINUX_DB.format(branch="update-stable"),
            MANJARO_DB.format(branch="testing"),
            COMMUNITY_DB.format(branch="testing"),
            COMMUNITY_DB.format(branch="stable"),
            COMMUNITY_DB.format(branch="extra"),
            BIGLINUX_DB.format(branch="testing"),
            BIGLINUX_DB.format(branch="stable"),
        ],
    ),
}


# What a user of each branch sees of BigCommunity's own packages, lowest
# priority first: a testing user also has stable, and pacman takes testing's
# copy when both have one. Moving the kernel from testing to stable must not
# look, to the watcher, as if testing had lost it.
VIEWS = {
    "stable": [COMMUNITY_DB.format(branch="stable")],
    "testing": [COMMUNITY_DB.format(branch="stable"), COMMUNITY_DB.format(branch="testing")],
}

# The branch stable modules are built from when stable's kernel is older than
# the PKGBUILD on main. build-package checks out branches by name, and falls
# back to main when the name does not exist, so it has to be a real branch.
STABLE_REF = "linux-big-stable"


def log(message):
    print(message, flush=True)


def fetch(url):
    # repo.communitybig.org refuses urllib's default User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def read_database(data):
    """Return {pkgname: 'pkgver-pkgrel'} from a pacman repository database."""
    versions = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            if not member.name.endswith("/desc"):
                continue
            fields, key = {}, None
            for line in archive.extractfile(member).read().decode().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key and key not in fields:
                    fields[key] = line
            if "NAME" in fields and "VERSION" in fields:
                versions[fields["NAME"]] = fields["VERSION"]
    return versions


class Tree:
    """The repository's files: as checked out, or as they were at a commit."""

    def __init__(self, root, commit=None):
        self.root, self.commit = root, commit

    def read(self, relative):
        if self.commit is None:
            with open(os.path.join(self.root, relative), encoding="utf-8") as source:
                return source.read()
        return subprocess.run(
            ["git", "-C", self.root, "show", f"{self.commit}:{relative}"],
            capture_output=True, text=True, check=True,
        ).stdout


def text_value(text, key, where):
    match = re.search(rf"^{key}=(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise SystemExit(f"{where}: no literal {key}=")
    return match.group(1).strip("'\"")


def pkgbuild_value(path, key):
    with open(path, encoding="utf-8") as pkgbuild:
        return text_value(pkgbuild.read(), key, path)


def kernel_version(text, where="linux-big/PKGBUILD"):
    return f"{text_value(text, 'pkgver', where)}-{text_value(text, 'pkgrel', where)}"


def find_commit(root, kernel):
    """The newest commit on this branch whose linux-big PKGBUILD is `kernel`.

    The last state of the repository for that kernel: what its modules were
    built from, with every module fix made before the next kernel.
    """
    try:
        commits = subprocess.run(
            ["git", "-C", root, "rev-list", "--first-parent", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    for commit in commits:
        try:
            text = Tree(root, commit).read("linux-big/PKGBUILD")
        except subprocess.CalledProcessError:
            continue
        try:
            if kernel_version(text) == kernel:
                return commit
        except SystemExit:
            continue
    return None


def rebuild_suffix(path, kernel):
    with open(path, encoding="utf-8") as pkgbuild:
        return rebuild_suffix_text(pkgbuild.read(), kernel)


def rebuild_suffix_text(text, kernel):
    """".N" when the module PKGBUILD sets _rebuild=N for this kernel, else "".

    Mirrors _pkgrel() in the module PKGBUILDs: a rebuild for the same kernel
    and driver gets a new version, and a new kernel drops it by itself.
    """
    values = {}
    for key in ("_rebuild_for", "_rebuild"):
        match = re.search(rf"^{key}=(\S*)\s*$", text, re.MULTILINE)
        values[key] = match.group(1).strip("'\"") if match else ""
    if values["_rebuild"] and values["_rebuild_for"] == kernel:
        return f".{values['_rebuild']}"
    return ""


def kernel_rel(pkgver, pkgrel):
    """The pkgrel the modules derive from the kernel: 7.2.7-2 -> 7020702."""
    major, minor, *rest = pkgver.split(".")
    patch = rest[0] if rest else "0"
    return f"{int(major)}{int(minor):02d}{int(patch):02d}{int(pkgrel):02d}"


def without_pkgrel(version):
    return version.rsplit("-", 1)[0]


def first_in(search, package, fetch_database):
    """The version of package in the first repository that has it, like pacman."""
    for url in search:
        version = fetch_database(url).get(package)
        if version:
            return version
    return None


# Branches the kernel itself is sent to when the PKGBUILD moves ahead of what
# is published. Only testing: moving a kernel to stable is a person's call.
KERNEL_BRANCHES = ("testing",)


def version_key(version):
    """A sortable key for a linux-big version such as 7.2.8-1."""
    pkgver, _, pkgrel = version.partition("-")
    return tuple(int(part) for part in pkgver.split(".")), int(pkgrel or 0)


def plan(root, fetch_database, refs=None, find=find_commit):
    """Return the builds to dispatch as (branch, manjaro_branch, module, expected).

    When stable's kernel is older than the PKGBUILD, its modules are planned
    from the commit that described that kernel, and refs["stable"] is set to
    it: the builds have to run from there.
    """
    head = Tree(root)
    kernel = kernel_version(head.read("linux-big/PKGBUILD"))
    log(f"linux-big in the repository: {kernel} (module pkgrel {kernel_rel(*kernel.split('-'))})")

    builds = []
    for branch, (manjaro_branch, _published_to, search) in BRANCHES.items():
        ours = {}
        for url in VIEWS[branch]:
            ours.update(fetch_database(url))
        published = ours.get("linux-big")

        tree, branch_kernel = head, kernel
        if branch not in KERNEL_BRANCHES and published is not None and published != kernel:
            commit = find(root, published)
            if commit is None:
                log(f"[{branch}] linux-big {published}: no commit describes it; its modules are not followed")
                continue
            tree, branch_kernel = Tree(root, commit), published
            if refs is not None:
                refs[branch] = commit
            log(f"[{branch}] linux-big {published} is behind the PKGBUILD; modules from {commit[:12]}")

        if published != branch_kernel:
            # The kernel watcher only commits the new version; building it is
            # decided here, from what is published, so a dispatch that failed
            # is retried on the next run instead of being forgotten.
            if branch in KERNEL_BRANCHES and (published is None or version_key(kernel) > version_key(published)):
                log(f"[{branch}] linux-big: published {published}, PKGBUILD is {kernel}; building it")
                builds.append((branch, manjaro_branch, "linux-big", kernel))
            else:
                log(f"[{branch}] linux-big {kernel} is not published (found {published}); modules wait for it")
            continue

        rel = kernel_rel(*branch_kernel.split("-"))
        for module, source in MODULES.items():
            where = f"{module}/PKGBUILD"
            try:
                text = tree.read(where)
            except (OSError, subprocess.CalledProcessError):
                log(f"[{branch}] {module}: not in the repository for linux-big {branch_kernel}, skipped")
                continue
            if source is None:
                driver = text_value(text, "pkgver", where)
            else:
                found = first_in(search, source, fetch_database)
                if found is None:
                    log(f"[{branch}] {module}: {source} is in none of the {branch} repositories, skipped")
                    continue
                driver = without_pkgrel(found)
            expected = f"{driver}-{rel}{rebuild_suffix_text(text, branch_kernel)}"
            current = ours.get(module)
            if current == expected:
                log(f"[{branch}] {module} {expected}: up to date")
            else:
                log(f"[{branch}] {module}: published {current}, expected {expected}")
                builds.append((branch, manjaro_branch, module, expected))
    return builds


def payload(branch, manjaro_branch, module, ref="main"):
    """The dispatch gitrepo sends for a package, pointed at one directory."""
    data = {
        "branch": ref,
        "branch_type": branch,
        "build_env": "normal",
        "url": REPOSITORY_URL,
        "tmate": False,
        "pkgbuild_dir": module,
        "manjaro_branch": manjaro_branch,
    }
    if branch == "testing":
        data["new_branch"] = "main"
    return {"event_type": module, "client_payload": data}


def pending(builds, state, force=False):
    """Drop the builds already dispatched for the same version.

    A build that fails would otherwise be dispatched again on every run; each
    expected version is dispatched once, and force retries it.
    """
    return [build for build in builds if force or state_key(*build) not in state]


def state_key(branch, _manjaro_branch, module, expected):
    return f"{branch}/{module}/{expected}"


def dispatch(token, body):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{WORKFLOW_REPOSITORY}/dispatches",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status


def building(token):
    """Packages build-package is building or has queued right now.

    A second guard besides the dispatch state: the state lives in the
    Actions cache, which a re-run or an eviction can lose, and a kernel
    dispatched twice is built twice, four hours each.
    """
    names = set()
    for status in ("queued", "in_progress", "waiting", "pending"):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if token:  # build-package is public: listing its runs works without one too
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{WORKFLOW_REPOSITORY}/actions/runs?status={status}&per_page=100",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            runs = json.load(response).get("workflow_runs", [])
        names.update(run.get("display_title", "") for run in runs)
    return names


def wait_for_kernel(expected, published, in_flight, sleep=time.sleep, now=time.monotonic,
                    minutes=330, poll=300, grace=1200):
    """Wait until linux-big `expected` is published in testing.

    Returns "published", "failed" when build-package is no longer building
    it and it is not published (after `grace` seconds, the time a dispatch
    takes to show up as a run), or "timeout".
    """
    start = now()
    while True:
        if published() == expected:
            return "published"
        waited = now() - start
        if waited >= grace and not in_flight():
            return "failed"
        if waited >= minutes * 60:
            return "timeout"
        sleep(poll)


def point_branch(root, name, commit):
    """Make branch `name` on GitHub point at `commit`, for build-package."""
    subprocess.run(["git", "-C", root, "push", "--force", "origin", f"{commit}:refs/heads/{name}"], check=True)
    log(f"branch {name} -> {commit[:12]}")


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as out:
        json.dump(state, out, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the big-kernel repository")
    parser.add_argument("--state", default="watch-state.json", help="builds already dispatched")
    parser.add_argument("--dry-run", action="store_true", help="report, dispatch nothing")
    parser.add_argument("--force", action="store_true", help="dispatch even what was dispatched before")
    parser.add_argument("--wait-for-kernel", metavar="VERSION", help="wait until this linux-big is in testing")
    args = parser.parse_args()

    if args.wait_for_kernel:
        token = os.environ.get("DISPATCH_TOKEN", "")
        testing = BRANCHES["testing"][1]
        result = wait_for_kernel(
            args.wait_for_kernel,
            published=lambda: read_database(fetch(testing)).get("linux-big"),
            in_flight=lambda: "linux-big" in building(token),
        )
        log(f"linux-big {args.wait_for_kernel}: {result}")
        return 0 if result == "published" else 1

    cache = {}

    def fetch_database(url):
        if url not in cache:
            cache[url] = read_database(fetch(url))
        return cache[url]

    refs = {}
    builds = plan(args.root, fetch_database, refs=refs)

    try:
        state = json.load(open(args.state, encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    todo = pending(builds, state, args.force)
    for build in builds:
        if build not in todo:
            log(f"[{build[0]}] {build[2]} {build[3]}: already dispatched at {state[state_key(*build)]}, not again")

    token = os.environ.get("DISPATCH_TOKEN", "")
    active = building(token) if todo and token and not args.dry_run else set()
    dispatched = 0
    pointed = set()
    for branch, manjaro_branch, module, expected in todo:
        if module in active:
            log(f"[{branch}] {module}: a build of it is already queued or running, not dispatched again")
            continue
        key = state_key(branch, manjaro_branch, module, expected)
        ref = "main"
        if branch in refs:
            ref = STABLE_REF
            if branch not in pointed and not args.dry_run:
                point_branch(args.root, STABLE_REF, refs[branch])
            pointed.add(branch)
        body = payload(branch, manjaro_branch, module, ref)
        if args.dry_run:
            log(f"[{branch}] would dispatch {module}: {json.dumps(body['client_payload'])}")
            continue
        if not token:
            raise SystemExit("DISPATCH_TOKEN is not set")
        status = dispatch(token, body)
        log(f"[{branch}] dispatched {module} {expected} (HTTP {status})")
        state[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dispatched += 1
        if module == "linux-big":
            # The workflow waits for it and then runs this watcher again, so
            # the modules follow the kernel instead of the next schedule.
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a", encoding="utf-8") as out:
                    out.write(f"kernel={expected}\n")
        # Saved after each one: if a later dispatch fails, the ones already
        # sent are not sent again.
        save_state(args.state, state)

    if not args.dry_run:
        save_state(args.state, state)
    log(f"{len(builds)} package(s) behind, {dispatched} dispatched")


if __name__ == "__main__":
    sys.exit(main())
