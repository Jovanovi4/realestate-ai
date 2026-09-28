"""Private SQLite + local media snapshots, with isolated restore verification.

Never overwrites the live database or prunes old backups. Other database/storage
backends need native backup tooling configured on the production server.
"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from django.apps import apps
from django.conf import settings
from django.db import connections, models
from django.utils import timezone


class BackupError(Exception):
    pass


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError("Invalid relative backup path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BackupError("Invalid relative backup path.")
    return Path(*path.parts)


def _regular_file(root, relative):
    relative = _safe_relative(relative)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BackupError("Symlinks are not allowed in CRM backups.")
    if not current.is_file() or not stat.S_ISREG(current.stat().st_mode):
        raise BackupError("A required backup file is missing or is not a regular file.")
    return current


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _inspect_database(path, file_fields):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.execute("PRAGMA trusted_schema=OFF")
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed.")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("CRM foreign key check failed.")
        tables = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if "django_migrations" not in tables or "properties_property" not in tables:
            raise BackupError("This is not a migrated RealtorAI database.")
        counts = {table: database.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0] for table in sorted(tables)}
        references = set()
        for table, column in file_fields:
            if table not in tables:
                raise BackupError("A required CRM table is missing.")
            for (name,) in database.execute(f"SELECT {_quote_identifier(column)} FROM {_quote_identifier(table)}"):
                if name:
                    _safe_relative(name)
                    references.add(name)
        return counts, sorted(references)


def _copy_private_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as reader, destination.open("xb") as writer:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(reader, writer, 1024 * 1024)


def _copy_media(source_root, destination_root):
    destination_root.mkdir(mode=0o700)
    if not source_root.exists():
        return
    if source_root.is_symlink() or not source_root.is_dir():
        raise BackupError("Local MEDIA_ROOT must be a real directory, not a symlink.")
    for directory, subdirectories, filenames in os.walk(source_root, followlinks=False):
        for name in subdirectories:
            if (Path(directory) / name).is_symlink():
                raise BackupError("Symlinks in MEDIA_ROOT are not supported.")
        for name in filenames:
            relative = (Path(directory) / name).relative_to(source_root).as_posix()
            _copy_private_file(_regular_file(source_root, relative), destination_root / _safe_relative(relative))


def _file_manifest(root):
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError("Symlinks are not allowed in CRM backups.")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        path = _regular_file(root, relative)
        files[relative] = {"size": path.stat().st_size, "sha256": _digest(path)}
    return files


def backup_root(value=None):
    raw = value or settings.CRM_BACKUP_ROOT
    if not raw:
        raise BackupError("Set CRM_BACKUP_ROOT or pass --output-dir.")
    requested = Path(raw).expanduser()
    if requested.is_symlink():
        raise BackupError("The backup directory must not be a symlink.")
    root = requested.resolve()
    for public_root in (settings.MEDIA_ROOT, settings.STATIC_ROOT, *settings.STATICFILES_DIRS):
        public_root = Path(public_root).resolve()
        if root == public_root or root.is_relative_to(public_root) or public_root.is_relative_to(root):
            raise BackupError("Keep backups separate from media and static directories.")
    return root


def create_backup(output_dir=None, *, using="default"):
    connection = connections[using]
    if connection.vendor != "sqlite":
        raise BackupError("This command supports SQLite only. Configure native PostgreSQL backups and media snapshots on your server.")
    if settings.STORAGES["default"]["BACKEND"] != "django.core.files.storage.FileSystemStorage":
        raise BackupError("This command supports local media only; configure backups for your storage provider.")
    if connection.in_atomic_block:
        raise BackupError("Run backups outside a database transaction.")
    root = backup_root(output_dir)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise BackupError("Cannot create a private backup directory; check permissions.") from error
    if root.stat().st_mode & 0o077:
        raise BackupError("The backup directory must be private (chmod 700).")
    if settings.CRM_BACKUP_TIMEOUT_SECONDS < 1:
        raise BackupError("CRM_BACKUP_TIMEOUT_SECONDS must be positive.")
    try:
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
    except OSError as error:
        raise BackupError("Cannot create a staging directory; check storage space and permissions.") from error
    final = root / f"snapshot-{timezone.now():%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:12]}"
    try:
        connection.ensure_connection()
        database_file = stage / "database.sqlite3"
        database_file.touch(mode=0o600)
        deadline = time.monotonic() + settings.CRM_BACKUP_TIMEOUT_SECONDS

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise BackupError("Database snapshot timed out; retry outside a busy period.")

        with closing(sqlite3.connect(database_file)) as target:
            connection.connection.backup(target, pages=256, progress=progress, sleep=.1)
        file_fields = [
            [model._meta.db_table, field.column]
            for model in apps.get_models()
            for field in model._meta.concrete_fields
            if isinstance(field, models.FileField)
        ]
        counts, references = _inspect_database(database_file, file_fields)
        _copy_media(Path(settings.MEDIA_ROOT).absolute(), stage / "media")
        for name in references:
            _regular_file(stage / "media", name)
        manifest = {
            "format_version": 1, "engine": "sqlite", "created_at": timezone.now().isoformat(),
            "file_fields": file_fields, "table_counts": counts, "media_references": references,
            "files": _file_manifest(stage),
        }
        with (stage / "manifest.json").open("x", encoding="utf-8") as stream:
            os.chmod(stage / "manifest.json", 0o600)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        verify_backup(stage)
        stage.rename(final)
        return final
    except (OSError, ValueError, sqlite3.Error) as error:
        raise BackupError("Could not create a complete CRM backup; check storage space, permissions and SQLite health.") from error
    finally:
        # Only this exact mkdtemp-created staging directory can be cleaned up.
        if stage.exists():
            shutil.rmtree(stage)


def latest_backup(output_dir=None):
    root = backup_root(output_dir)
    candidates = sorted((path for path in root.glob("snapshot-*") if not path.is_symlink() and path.is_dir() and (path / "manifest.json").is_file()), reverse=True)
    if not candidates:
        raise BackupError("No completed CRM backups found.")
    return candidates[0]


def verify_backup(snapshot):
    """Actually restore DB and media into a fresh temp directory, then verify."""
    requested = Path(snapshot)
    if requested.is_symlink():
        raise BackupError("A snapshot must not be a symlink.")
    root = requested.resolve()
    try:
        with _regular_file(root, "manifest.json").open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or manifest.get("format_version") != 1 or manifest.get("engine") != "sqlite":
            raise BackupError("Unsupported CRM backup format.")
        file_fields = manifest["file_fields"]
        if not isinstance(file_fields, list) or any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) for value in pair) for pair in file_fields):
            raise BackupError("Invalid backup schema metadata.")
        files = manifest["files"]
        if "database.sqlite3" not in files or _file_manifest(root) != files:
            raise BackupError("Backup checksum/file list verification failed.")
        with tempfile.TemporaryDirectory(prefix="realtorai-restore-check-") as directory:
            restored = Path(directory)
            (restored / "media").mkdir(mode=0o700)
            for relative in files:
                if relative != "database.sqlite3" and not relative.startswith("media/"):
                    raise BackupError("Unexpected file in CRM backup.")
                _copy_private_file(_regular_file(root, relative), restored / _safe_relative(relative))
            if _file_manifest(restored) != files:
                raise BackupError("Restored file checksum verification failed.")
            counts, references = _inspect_database(restored / "database.sqlite3", manifest["file_fields"])
            if counts != manifest["table_counts"] or references != manifest["media_references"]:
                raise BackupError("Restored CRM records do not match the snapshot.")
            for name in references:
                _regular_file(restored / "media", name)
        return {"tables": len(counts), "media_files": len(files) - 1, "media_references": len(references)}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise BackupError("Invalid or incomplete CRM backup.") from error
