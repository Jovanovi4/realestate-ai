import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name("backup_offsite_pilot.sh")


class OffsiteBackupFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        (self.scripts / "backup_offsite_pilot.sh").write_bytes(SOURCE.read_bytes())
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.password = self.root / "password"
        self.password.write_text("test password")
        self.write_executable(self.bin / "flock", "#!/bin/sh\nexit 0\n")
        self.write_executable(self.bin / "restic", """#!/bin/sh
echo "$1" >> "$TEST_CALLS"
if [ "$1" = backup ]; then
    echo '{"message_type":"summary","snapshot_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
fi
""")
        (self.scripts / "prune_verified_pilot_backups.py").write_text("pass\n")

    @staticmethod
    def write_executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_backup(self):
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{self.bin}:{environment['PATH']}",
            "REAICRM_AUTOMATED_BACKUP_ROOT": str(self.root / "backups"),
            "RESTIC_REPOSITORY": "s3:https://storage.example.test/bucket/repo",
            "RESTIC_PASSWORD_FILE": str(self.password),
            "TEST_CALLS": str(self.calls),
        })
        return subprocess.run(
            ["bash", str(self.scripts / "backup_offsite_pilot.sh")],
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_unverified_local_copy_never_reaches_remote(self):
        self.write_executable(self.scripts / "backup_verify_pilot.sh", """#!/bin/sh
mkdir -p "$1/pilot-20261008T000000Z-test"
echo "$1/pilot-20261008T000000Z-test" > "$2"
""")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_verified_copy_is_uploaded_checked_and_marked(self):
        self.write_executable(self.scripts / "backup_verify_pilot.sh", """#!/bin/sh
mkdir -p "$1/pilot-20261008T000000Z-test"
touch "$1/pilot-20261008T000000Z-test/VERIFIED"
echo "$1/pilot-20261008T000000Z-test" > "$2"
""")

        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "backup", "check", "forget", "check",
        ])
        self.assertTrue((self.root / "backups/pilot-20261008T000000Z-test/OFFSITE").is_file())


if __name__ == "__main__":
    unittest.main()
