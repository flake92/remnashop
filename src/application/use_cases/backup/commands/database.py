import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Final

from loguru import logger

from src.application.common import Interactor, Notifier
from src.application.common.dao import SettingsDao
from src.application.common.policy import Permission
from src.application.dto import MediaDescriptorDto, MessagePayloadDto, UserDto
from src.core.constants import BACKUP_DIR, DATETIME_FILE_FORMAT
from src.core.enums import MediaType, Role
from src.core.utils.time import datetime_now

from .archive import compress_dump, split_for_telegram
from .dump import CreateDatabaseDump

# Auto backups reach admins through the notification worker, so the parts must outlive
# this task; the next run clears them.
OUTGOING_DIR: Final[Path] = BACKUP_DIR / "outgoing"


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / (1024 * 1024):.1f}"


def _document_payload(file: Path, disable_default_markup: bool) -> MessagePayloadDto:
    return MessagePayloadDto(
        i18n_key="",
        media=MediaDescriptorDto(kind="fs", value=str(file), filename=file.name),
        media_type=MediaType.DOCUMENT,
        delete_after=None,
        disable_default_markup=disable_default_markup,
    )


class AutoBackupDatabase(Interactor[None, None]):
    required_permission = None

    def __init__(
        self,
        settings_dao: SettingsDao,
        notifier: Notifier,
        create_database_dump: CreateDatabaseDump,
    ) -> None:
        self.settings_dao = settings_dao
        self.notifier = notifier
        self.create_database_dump = create_database_dump

    async def _execute(self, actor: UserDto, data: None) -> None:
        settings = await self.settings_dao.get()
        backup_cfg = settings.backup

        if not backup_cfg.enabled:
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        # Both the old plain `.sql` files and the new `.sql.gz` archives take part in rotation.
        existing = sorted(BACKUP_DIR.glob("db_backup_*.sql*"), key=lambda f: f.stat().st_mtime)

        if existing:
            elapsed_hours = (datetime_now().timestamp() - existing[-1].stat().st_mtime) / 3600
            if elapsed_hours < backup_cfg.interval_hours:
                return

        timestamp = datetime_now().strftime(DATETIME_FILE_FORMAT)
        tmp_dir = Path(tempfile.mkdtemp())
        # Parts of the previous run were already handed to the notification worker long ago.
        shutil.rmtree(OUTGOING_DIR, ignore_errors=True)

        try:
            dump_file = tmp_dir / f"db_backup_{timestamp}.sql"
            await self.create_database_dump.system(dump_file)

            final_path = BACKUP_DIR / f"{dump_file.name}.gz"
            await asyncio.to_thread(compress_dump, dump_file, final_path)

            self._rotate(existing, backup_cfg.max_files)
            logger.info(
                f"Auto backup created: {final_path.name} "
                f"({_mb(dump_file)} MB dump, {_mb(final_path)} MB compressed)"
            )

            if backup_cfg.send_to_chat:
                try:
                    files = await asyncio.to_thread(split_for_telegram, final_path, OUTGOING_DIR)
                    if len(files) > 1:
                        logger.info(
                            f"Backup {final_path.name} is sent in {len(files)} parts; "
                            f"join them with: cat {final_path.name}.0* > {final_path.name}"
                        )
                    for file in files:
                        await self.notifier.notify_admins(
                            payload=_document_payload(file, disable_default_markup=True),
                            roles=[Role.OWNER, Role.DEV],
                        )
                except Exception as e:
                    logger.warning(f"Failed to send backup to admins: {e}")

        except Exception as e:
            logger.exception(f"Auto backup failed: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _rotate(files: list[Path], max_files: int) -> None:
        while len(files) >= max_files:
            files.pop(0).unlink(missing_ok=True)


class BackupDatabase(Interactor[None, None]):
    required_permission = Permission.SETTINGS_BACKUP

    def __init__(self, notifier: Notifier, create_database_dump: CreateDatabaseDump) -> None:
        self.notifier = notifier
        self.create_database_dump = create_database_dump

    async def _execute(self, actor: UserDto, data: None) -> None:
        await self.notifier.notify_user(actor, i18n_key="ntf-backup.db-started")

        tmp_dir = Path(tempfile.mkdtemp())
        try:
            timestamp = datetime_now().strftime(DATETIME_FILE_FORMAT)
            dump_file = tmp_dir / f"db_backup_{timestamp}.sql"

            await self.create_database_dump.system(dump_file)

            archive = tmp_dir / f"{dump_file.name}.gz"
            await asyncio.to_thread(compress_dump, dump_file, archive)
            files = await asyncio.to_thread(split_for_telegram, archive, tmp_dir / "parts")

            for index, file in enumerate(files):
                await self.notifier.notify_user(
                    user=actor,
                    payload=_document_payload(
                        file,
                        # The menu keyboard goes under the last part only.
                        disable_default_markup=index < len(files) - 1,
                    ),
                )
            logger.info(
                f"{actor.log} Database backup sent: {archive.name} "
                f"({_mb(archive)} MB, {len(files)} file(s))"
            )
        except Exception as e:
            logger.exception(f"{actor.log} Failed to create database backup: {e}")
            await self.notifier.notify_user(actor, i18n_key="ntf-backup.error")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
