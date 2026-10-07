"""Restore a pilot media archive into a temporary directory and verify its files."""

import hashlib
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile


def _parts(name):
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part != ".")
    if path.is_absolute() or ".." in parts or "\\" in name:
        raise ValueError("Unsafe media archive path")
    return parts


def _digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.digest()


def verify_archive(archive_path, references_path):
    seen = set()
    files = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            parts = _parts(member.name)
            if not (member.isfile() or member.isdir()) or (not parts and not member.isdir()):
                raise ValueError("Unsupported media archive entry")
            if parts in seen and parts:
                raise ValueError("Duplicate media archive path")
            seen.add(parts)
            if member.isfile():
                files.add("/".join(parts))

        references = [line for line in Path(references_path).read_text().splitlines() if line]
        for reference in references:
            name = "/".join(_parts(reference))
            if name not in files:
                raise ValueError("Database references a file absent from the media archive")

        with tempfile.TemporaryDirectory(prefix="reaicrm-media-restore-") as temporary:
            archive.extractall(temporary, members=members, filter="data")
            count = size = 0
            for member in members:
                if not member.isfile():
                    continue
                restored = Path(temporary).joinpath(*_parts(member.name))
                with archive.extractfile(member) as original, restored.open("rb") as extracted:
                    if _digest(original) != _digest(extracted):
                        raise ValueError("Restored media differs from its archive entry")
                count += 1
                size += member.size
    return count, size, len(references)


if __name__ == "__main__":
    try:
        file_count, byte_count, reference_count = verify_archive(sys.argv[1], sys.argv[2])
    except (IndexError, OSError, tarfile.TarError, ValueError) as error:
        print(f"Media restore verification failed ({type(error).__name__}).", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Media restored: {file_count} files, {byte_count} bytes; {reference_count} database references found.")
