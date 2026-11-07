import gzip
import shutil
from pathlib import Path
from typing import Final

# The Bot API refuses documents above 50 MB with "Request Entity Too Large".
# Parts stay well below it so multipart overhead never tips a part over.
TELEGRAM_DOCUMENT_LIMIT: Final[int] = 50 * 1024 * 1024
BACKUP_PART_SIZE: Final[int] = 45 * 1024 * 1024
_COPY_CHUNK: Final[int] = 1024 * 1024


def compress_dump(source: Path, target: Path) -> Path:
    """Gzip a plain SQL dump; a text dump usually shrinks five to ten times."""
    with source.open("rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, _COPY_CHUNK)
    return target


def split_for_telegram(
    archive: Path,
    parts_dir: Path,
    part_size: int = BACKUP_PART_SIZE,
) -> list[Path]:
    """Return files that each fit in one Telegram document.

    An archive under the limit is returned as is. A larger one is cut into
    `<name>.001`, `<name>.002`, … in `parts_dir`; joining them in order with
    `cat <name>.0* > <name>` gives back the original archive byte for byte.
    """
    if archive.stat().st_size <= part_size:
        return [archive]

    parts_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    with archive.open("rb") as src:
        index = 1
        while True:
            part = parts_dir / f"{archive.name}.{index:03d}"
            written = 0
            with part.open("wb") as dst:
                while written < part_size:
                    chunk = src.read(min(_COPY_CHUNK, part_size - written))
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            if written < part_size:
                break
            index += 1
    return parts
