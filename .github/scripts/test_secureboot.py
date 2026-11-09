"""Every package of every series is signed for Secure Boot.

A user who enrolled the BigCommunity key boots the "modo jogos" entry under
lockdown, where a module that is not signed does not load: one package that
forgets to sign leaves them without that driver. The signing itself is
secureboot/sign.sh; this checks every PKGBUILD calls it, and in the right
place, so a new series or module cannot ship without it.

Run from the repository root: python3 -m unittest discover -s .github/scripts
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SOURCE = "source ../secureboot/sign.sh"
CERT = os.path.join(ROOT, "secureboot", "bigcommunity-secureboot-2026.crt")


def pkgbuilds(pattern):
    for name in sorted(os.listdir(ROOT)):
        path = os.path.join(ROOT, name, "PKGBUILD")
        if re.fullmatch(pattern, name) and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                yield name, f.read()


def function(text, name):
    match = re.search(rf"^{re.escape(name)}\(\) {{\n(.*?)^}}", text, re.M | re.S)
    return match.group(1) if match else ""


class TestKernels(unittest.TestCase):
    def test_the_kernel_image_is_signed_once_installed(self):
        kernels = list(pkgbuilds(r"linux-big[0-9]+"))
        self.assertTrue(kernels)
        for name, text in kernels:
            with self.subTest(name):
                self.assertIn(SOURCE, text)
                body = function(text, "_package")
                install = body.index('"$modulesdir/vmlinuz"\n')
                self.assertTrue(body[install:].split("\n", 2)[1].strip() == '_sb_sign_kernel "$modulesdir/vmlinuz"',
                                "_sb_sign_kernel must follow the vmlinuz install")

    def test_sbsigntools_is_needed_only_with_a_key(self):
        for name, text in pkgbuilds(r"linux-big[0-9]+"):
            with self.subTest(name):
                self.assertNotRegex(text, r"(?m)^\s+sbsigntools$", "sbsigntools must not be an unconditional makedepend")
                self.assertIn("makedepends+=(sbsigntools)", text)


class TestModules(unittest.TestCase):
    def test_every_module_is_signed_before_it_is_compressed(self):
        modules = list(pkgbuilds(r"linux-big[0-9]+-.+"))
        self.assertTrue(modules)
        for name, text in modules:
            with self.subTest(name):
                self.assertIn(SOURCE, text)
                body = function(text, "package")
                self.assertIn('_sb_sign_modules "$pkgdir" "$_kernver"', body)
                sign = body.index("_sb_sign_modules")
                for compress in re.finditer(r"-exec (zstd|xz|gzip)\b", body):
                    self.assertLess(sign, compress.start(), "modules must be signed before compression")


class TestCertificate(unittest.TestCase):
    def test_the_certificate_names_the_signer_sign_sh_checks(self):
        with open(os.path.join(ROOT, "secureboot", "sign.sh"), encoding="utf-8") as f:
            signer = re.search(r'^_sb_signer="(.*)"$', f.read(), re.M).group(1)
        subject = subprocess.run(["openssl", "x509", "-in", CERT, "-noout", "-subject", "-nameopt", "multiline"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertRegex(subject, rf"commonName\s+= {re.escape(signer)}\n")


class TestPackage(unittest.TestCase):
    """big-secureboot: what the package ships is there and consistent."""

    DIR = os.path.join(ROOT, "big-secureboot")

    def setUp(self):
        with open(os.path.join(self.DIR, "PKGBUILD"), encoding="utf-8") as f:
            self.text = f.read()

    def array(self, name):
        body = re.search(rf"^{name}=\((.*?)\)", self.text, re.M | re.S).group(1)
        return re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", body)

    def test_every_local_source_exists_and_has_a_checksum(self):
        sources = [next(v for v in m if v) for m in self.array("source")]
        sums = [next(v for v in m if v) for m in self.array("sha256sums")]
        self.assertEqual(len(sources), len(sums))
        for source in sources:
            if "://" not in source:
                self.assertTrue(os.path.isfile(os.path.join(self.DIR, source)), source)

    def test_the_install_script_runs_commands_the_tool_has(self):
        with open(os.path.join(self.DIR, "big-secureboot"), encoding="utf-8") as f:
            tool = f.read()
        commands = set(re.findall(r"^  (\w+)\)", tool, re.M))
        options = set(re.findall(r"^      (--[\w-]+)\)", tool, re.M))
        self.assertIn("install=big-secureboot.install", self.text)
        with open(os.path.join(self.DIR, "big-secureboot.install"), encoding="utf-8") as f:
            script = f.read()
        for name in ("post_install", "post_upgrade", "pre_remove"):
            self.assertIn(name + "()", script)
        for command, option in re.findall(r"/usr/bin/big-secureboot (\w+) ?(--[\w-]+)?", script):
            self.assertIn(command, commands)
            if option:
                self.assertIn(option, options)

    def test_the_grub_has_no_serial_command(self):
        modules = re.search(r"^_grub_modules=\((.*?)\)", self.text, re.M | re.S).group(1).split()
        self.assertNotIn("serial", modules, "CVE-2026-97876 went through the serial command")
        for needed in ("btrfs", "luks2", "argon2", "tpm", "gfxmenu", "png"):
            self.assertIn(needed, modules)

    def test_the_games_entries_carry_the_anti_cheat_options(self):
        with open(os.path.join(self.DIR, "11_big-secureboot"), encoding="utf-8") as f:
            script = f.read()
        for option in ("lockdown=integrity", "sysctl.kernel.kexec_load_disabled=1", "iommu.passthrough=0"):
            self.assertIn(option, script)


if __name__ == "__main__":
    unittest.main()
