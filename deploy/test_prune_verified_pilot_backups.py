import tempfile
import unittest
from pathlib import Path

from deploy.prune_verified_pilot_backups import prune


class LocalBackupRetentionTests(unittest.TestCase):
    def test_keeps_three_offsite_copies_and_preserves_unuploaded_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in range(1, 6):
                backup = root / f"pilot-202610{day:02d}T000000Z-test"
                backup.mkdir()
                (backup / "VERIFIED").write_text("verified")
                (backup / "OFFSITE").write_text("snapshot")
            unuploaded = root / "pilot-20260930T000000Z-unuploaded"
            unuploaded.mkdir()
            (unuploaded / "VERIFIED").write_text("verified")

            removed = prune(root, 3)

            self.assertEqual([path.name for path in removed], [
                "pilot-20261002T000000Z-test", "pilot-20261001T000000Z-test",
            ])
            self.assertTrue(unuploaded.exists())
            self.assertEqual(len(list(root.glob("pilot-*"))), 4)

    def test_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                prune(link, 3)


if __name__ == "__main__":
    unittest.main()
