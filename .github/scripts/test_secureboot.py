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


if __name__ == "__main__":
    unittest.main()
