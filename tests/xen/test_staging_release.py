import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("staging-release.sh").read_text()


class StagingReleaseTests(unittest.TestCase):
    def run_cleanup(self, directory, *, root=None, image="nixos.raw", prefix=""):
        env = {"PATH": os.environ["PATH"], "root": str(root or directory / "root"),
               "diskImage": image}
        return subprocess.run(["bash", "-e", "-c", prefix + SCRIPT], cwd=directory,
                              env=env, capture_output=True, text=True, timeout=10)

    def fixture(self, directory):
        root = directory / "root"
        root.mkdir()
        (root / "payload").write_text("already copied")
        image = directory / "nixos.raw"
        image.write_bytes(b"synthetic raw image")
        return root, image

    def test_removes_only_staging_and_preserves_external_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, image = self.fixture(directory)
            external = directory / "outside"
            external.mkdir()
            (external / "sentinel").write_text("untouched")
            external.chmod(0o500)
            (root / "external").symlink_to(external, target_is_directory=True)
            root.chmod(0o500)
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            try:
                result = self.run_cleanup(directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(root.exists())
                self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), digest)
                self.assertEqual((external / "sentinel").read_text(), "untouched")
                self.assertEqual(external.stat().st_mode & 0o777, 0o500)
            finally:
                external.chmod(0o700)
                if root.exists():
                    root.chmod(0o700)

    def test_missing_raw_image_retains_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, image = self.fixture(directory)
            image.unlink()
            self.assertNotEqual(self.run_cleanup(directory).returncode, 0)
            self.assertTrue((root / "payload").exists())

    def test_raw_image_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, image = self.fixture(directory)
            image.unlink()
            image.symlink_to(root / "payload")
            self.assertNotEqual(self.run_cleanup(directory).returncode, 0)
            self.assertTrue((root / "payload").exists())

    def test_staging_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, _ = self.fixture(directory)
            outside = directory / "outside"
            root.rename(outside)
            root.symlink_to(outside, target_is_directory=True)
            self.assertNotEqual(self.run_cleanup(directory).returncode, 0)
            self.assertTrue((outside / "payload").exists())

    def test_alternate_staging_or_raw_path_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, _ = self.fixture(directory)
            self.assertNotEqual(self.run_cleanup(directory, root=directory).returncode, 0)
            self.assertNotEqual(self.run_cleanup(directory, image="other.raw").returncode, 0)
            self.assertTrue((root / "payload").exists())

    def test_failed_copy_does_not_run_following_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root, _ = self.fixture(directory)
            result = self.run_cleanup(directory, prefix="false || (exit 1)\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "payload").exists())


if __name__ == "__main__":
    unittest.main()
