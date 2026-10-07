import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from deploy.verify_media_archive import verify_archive


class MediaArchiveRestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / "media.tar.gz"
        self.references = Path(self.directory.name) / "references.txt"

    def write_archive(self, *entries):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in entries:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))

    def test_restores_bytes_and_checks_every_database_reference(self):
        self.write_archive(("./properties/home.jpg", b"photo"), ("./realtors/owner.png", b"avatar"))
        self.references.write_text("properties/home.jpg\nrealtors/owner.png\n")
        self.assertEqual(verify_archive(self.archive, self.references), (2, 11, 2))

    def test_missing_referenced_file_fails_verification(self):
        self.write_archive(("./properties/home.jpg", b"photo"))
        self.references.write_text("properties/missing.jpg\n")
        with self.assertRaises(ValueError):
            verify_archive(self.archive, self.references)

    def test_symlinks_and_path_traversal_are_rejected(self):
        self.references.write_text("")
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("./properties/linked.jpg")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
        with self.assertRaises(ValueError):
            verify_archive(self.archive, self.references)
        self.write_archive(("../outside", b"bad"))
        with self.assertRaises(ValueError):
            verify_archive(self.archive, self.references)


if __name__ == "__main__":
    unittest.main()
