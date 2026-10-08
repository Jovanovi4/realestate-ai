"""Keep a few local verified copies that have also reached offsite storage."""

import argparse
from pathlib import Path
import shutil


def prune(root: Path, keep: int) -> list[Path]:
    if not root.is_absolute() or root == Path("/") or root.is_symlink() or not root.is_dir() or keep < 2:
        raise ValueError("Invalid backup root or retention count")
    eligible = sorted(
        (
            path for path in root.glob("pilot-*")
            if path.is_dir() and not path.is_symlink()
            and (path / "VERIFIED").is_file() and (path / "OFFSITE").is_file()
            and not (path / "VERIFIED").is_symlink() and not (path / "OFFSITE").is_symlink()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = []
    for path in eligible[keep:]:
        shutil.rmtree(path)
        removed.append(path)
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--keep", type=int, default=3)
    arguments = parser.parse_args()
    for removed in prune(arguments.root, arguments.keep):
        print(f"Removed older verified local backup: {removed}")
