"""Installing Fluigent's SDK from a release archive, and choosing which copy to load."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from admet.engines.acquisition.fluidics.sdk import FluigentSDK, install_sdk


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.archive = self.root / "fgt-SDK.zip"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("fgt-SDK-24.0.0/Python/Fluigent/__init__.py", "")
            bundle.writestr("fgt-SDK-24.0.0/Python/Fluigent/SDK/__init__.py", '__version__ = "24.0.0"\n')
            bundle.writestr("fgt-SDK-24.0.0/Python/Fluigent/SDK/shared/windows/x64/fgt_SDK.dll", "dll")
            bundle.writestr("fgt-SDK-24.0.0/C++/fgt_SDK.h", "")
        self.target = self.root / "vendor" / "Python"

    def install(self, **kwargs):
        return install_sdk("24.0.0", target=self.target, source=self.archive.as_uri(), **kwargs)

    def test_only_the_python_package_is_unpacked(self):
        self.assertEqual(self.install(), self.target)

        files = sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())
        self.assertEqual(files, ["Fluigent/SDK/__init__.py", "Fluigent/SDK/shared/windows/x64/fgt_SDK.dll",
                                 "Fluigent/__init__.py"])

    def test_an_installed_sdk_is_replaced_only_with_force(self):
        self.install()
        (self.target / "Fluigent" / "stale.py").write_text("")

        with self.assertRaises(FileExistsError):
            self.install()
        self.install(force=True)
        self.assertFalse((self.target / "Fluigent" / "stale.py").exists())


class PathTests(unittest.TestCase):
    def test_a_wrong_sdk_path_is_reported_not_skipped(self):
        with tempfile.TemporaryDirectory() as empty, patch.dict(os.environ, {"ADMET_FLUIGENT_SDK_PATH": empty}):
            availability = FluigentSDK().preflight()

        self.assertFalse(availability.available)
        self.assertIn("has no Fluigent/SDK folder", availability.message)


if __name__ == "__main__":
    unittest.main()
