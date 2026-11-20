import gzip
import os
from pathlib import Path

from src.application.use_cases.backup.commands.archive import (
    BACKUP_PART_SIZE,
    TELEGRAM_DOCUMENT_LIMIT,
    compress_dump,
    split_for_telegram,
)


def test_compressed_dump_restores_to_the_original(tmp_path: Path) -> None:
    dump = tmp_path / "db_backup.sql"
    dump.write_text("INSERT INTO users VALUES (1, 'a');\n" * 20_000)

    archive = compress_dump(dump, tmp_path / "db_backup.sql.gz")

    assert archive.stat().st_size < dump.stat().st_size / 5
    assert gzip.decompress(archive.read_bytes()) == dump.read_bytes()


def test_small_archive_is_sent_as_one_file(tmp_path: Path) -> None:
    archive = tmp_path / "db_backup.sql.gz"
    archive.write_bytes(b"x" * 100)

    assert split_for_telegram(archive, tmp_path / "parts", part_size=100) == [archive]
    assert not (tmp_path / "parts").exists()


def test_large_archive_is_cut_into_parts_that_join_back(tmp_path: Path) -> None:
    archive = tmp_path / "db_backup.sql.gz"
    payload = os.urandom(250)
    archive.write_bytes(payload)

    parts = split_for_telegram(archive, tmp_path / "parts", part_size=100)

    assert [p.name for p in parts] == [
        "db_backup.sql.gz.001",
        "db_backup.sql.gz.002",
        "db_backup.sql.gz.003",
    ]
    assert [p.stat().st_size for p in parts] == [100, 100, 50]
    assert b"".join(p.read_bytes() for p in parts) == payload


def test_exact_multiple_leaves_no_empty_trailing_part(tmp_path: Path) -> None:
    archive = tmp_path / "db_backup.sql.gz"
    archive.write_bytes(os.urandom(200))

    parts = split_for_telegram(archive, tmp_path / "parts", part_size=100)

    assert len(parts) == 2
    assert sorted((tmp_path / "parts").iterdir()) == parts


def test_default_part_size_fits_the_bot_api_limit() -> None:
    assert BACKUP_PART_SIZE < TELEGRAM_DOCUMENT_LIMIT
