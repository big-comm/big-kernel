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
import sys
import tarfile
import time
import urllib.request

REPOSITORY_URL = "https://github.com/big-comm/big-kernel"
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


def log(message):
    print(message, flush=True)


def fetch(url):
    # repo.communitybig.org refuses urllib's default User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": "big-kernel-watch/1 (+%s)" % REPOSITORY_URL})
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


def pkgbuild_value(path, key):
    with open(path, encoding="utf-8") as pkgbuild:
        match = re.search(rf"^{key}=(\S+)\s*$", pkgbuild.read(), re.MULTILINE)
    if not match:
        raise SystemExit(f"{path}: no literal {key}=")
    return match.group(1).strip("'\"")


def rebuild_suffix(path, kernel):
    """".N" when the module PKGBUILD sets _rebuild=N for this kernel, else "".

    Mirrors _pkgrel() in the module PKGBUILDs: a rebuild for the same kernel
    and driver gets a new version, and a new kernel drops it by itself.
    """
    with open(path, encoding="utf-8") as pkgbuild:
        text = pkgbuild.read()
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


def plan(root, fetch_database):
    """Return the builds to dispatch as (branch, manjaro_branch, module, expected)."""
    kernel_pkgbuild = os.path.join(root, "linux-big", "PKGBUILD")
    pkgver = pkgbuild_value(kernel_pkgbuild, "pkgver")
    pkgrel = pkgbuild_value(kernel_pkgbuild, "pkgrel")
    kernel = f"{pkgver}-{pkgrel}"
    rel = kernel_rel(pkgver, pkgrel)
    log(f"linux-big in the repository: {kernel} (module pkgrel {rel})")

    builds = []
    for branch, (manjaro_branch, ours_url, search) in BRANCHES.items():
        ours = fetch_database(ours_url)
        published = ours.get("linux-big")
        if published != kernel:
            log(f"[{branch}] linux-big {kernel} is not published (found {published}); modules wait for it")
            continue
        for module, source in MODULES.items():
            if source is None:
                driver = pkgbuild_value(os.path.join(root, module, "PKGBUILD"), "pkgver")
            else:
                found = first_in(search, source, fetch_database)
                if found is None:
                    log(f"[{branch}] {module}: {source} is in none of the {branch} repositories, skipped")
                    continue
                driver = without_pkgrel(found)
            suffix = rebuild_suffix(os.path.join(root, module, "PKGBUILD"), kernel)
            expected = f"{driver}-{rel}{suffix}"
            current = ours.get(module)
            if current == expected:
                log(f"[{branch}] {module} {expected}: up to date")
            else:
                log(f"[{branch}] {module}: published {current}, expected {expected}")
                builds.append((branch, manjaro_branch, module, expected))
    return builds


def payload(branch, manjaro_branch, module):
    """The dispatch gitrepo sends for a package, pointed at one directory."""
    data = {
        "branch": "main",
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the big-kernel repository")
    parser.add_argument("--state", default="watch-state.json", help="builds already dispatched")
    parser.add_argument("--dry-run", action="store_true", help="report, dispatch nothing")
    parser.add_argument("--force", action="store_true", help="dispatch even what was dispatched before")
    args = parser.parse_args()

    cache = {}

    def fetch_database(url):
        if url not in cache:
            cache[url] = read_database(fetch(url))
        return cache[url]

    builds = plan(args.root, fetch_database)

    try:
        state = json.load(open(args.state, encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    todo = pending(builds, state, args.force)
    for build in builds:
        if build not in todo:
            log(f"[{build[0]}] {build[2]} {build[3]}: already dispatched at {state[state_key(*build)]}, not again")

    token = os.environ.get("DISPATCH_TOKEN", "")
    dispatched = 0
    for branch, manjaro_branch, module, expected in todo:
        key = state_key(branch, manjaro_branch, module, expected)
        body = payload(branch, manjaro_branch, module)
        if args.dry_run:
            log(f"[{branch}] would dispatch {module}: {json.dumps(body['client_payload'])}")
            continue
        if not token:
            raise SystemExit("DISPATCH_TOKEN is not set")
        status = dispatch(token, body)
        log(f"[{branch}] dispatched {module} {expected} (HTTP {status})")
        state[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dispatched += 1

    if not args.dry_run:
        json.dump(state, open(args.state, "w", encoding="utf-8"), indent=2, sort_keys=True)
    log(f"{len(builds)} module(s) behind, {dispatched} dispatched")


if __name__ == "__main__":
    sys.exit(main())
