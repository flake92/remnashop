import asyncio
from typing import Optional, cast

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger

from src.application.common import BroadcastExecutionLock, Notifier
from src.application.common.dao import BroadcastDao
from src.application.dto import BroadcastDto, BroadcastMessageDto, MessagePayloadDto, UserDto
from src.application.use_cases.broadcast.commands.lifecycle import (
    FinishBroadcast,
    FinishBroadcastDto,
)
from src.application.use_cases.broadcast.commands.messages import (
    BulkUpdateBroadcastMessages,
    InitializeBroadcastMessages,
    InitializeBroadcastMessagesDto,
    UpdateBroadcastMessageStatus,
    UpdateBroadcastMessageStatusDto,
)
from src.application.use_cases.broadcast.queries.audience import (
    GetBroadcastAudienceUsers,
    GetBroadcastAudienceUsersDto,
)
from src.application.use_cases.misc.commands.maintenance import ClearOldBroadcasts
from src.core.constants import BATCH_DELAY, BATCH_SIZE_20
from src.core.enums import BroadcastMessageStatus, BroadcastStatus
from src.core.utils.iterables import chunked
from src.infrastructure.taskiq.broker import broker

BROADCAST_MAX_RETRY_WAIT_SECONDS = 10 * 60


def _pending_broadcast_users(
    users: list[UserDto],
    messages: list[BroadcastMessageDto],
) -> list[UserDto]:
    message_by_user_id = {message.user_id: message for message in messages}
    return [
        user
        for user in users
        if (message := message_by_user_id.get(user.id)) is not None
        and message.status == BroadcastMessageStatus.PENDING
    ]


async def _send_broadcast_locked(  # noqa: C901
    broadcast: BroadcastDto,
    plan_id: Optional[int],
    broadcast_dao: FromDishka[BroadcastDao],
    get_broadcast_audience_users: FromDishka[GetBroadcastAudienceUsers],
    initialize_broadcast_messages: FromDishka[InitializeBroadcastMessages],
    update_broadcast_message_status: FromDishka[UpdateBroadcastMessageStatus],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
) -> None:
    task_id = broadcast.task_id

    try:
        current_broadcast = await broadcast_dao.get_by_task_id(task_id)
        if current_broadcast is None:
            logger.warning(f"Broadcast '{task_id}' no longer exists; skipping task")
            return
        if current_broadcast.status != BroadcastStatus.PROCESSING:
            logger.info(
                f"Broadcast '{task_id}' is already '{current_broadcast.status}'; "
                "skipping duplicate task"
            )
            return

        users = await get_broadcast_audience_users.system(
            GetBroadcastAudienceUsersDto(current_broadcast.audience, plan_id)
        )

        # Broadcast is Telegram-only; exclude web-only users without a telegram_id
        users = [u for u in users if u.telegram_id is not None]

        if not users:
            logger.warning(f"No users found for broadcast '{task_id}'")
            await finish_broadcast.system(FinishBroadcastDto(task_id, BroadcastStatus.COMPLETED))
            return

        messages = current_broadcast.messages
        if not messages:
            messages = []
            for user in users:
                messages.append(
                    BroadcastMessageDto(
                        user_id=user.id,
                        user_telegram_id=user.telegram_id,
                        status=BroadcastMessageStatus.PENDING,
                    )
                )

            messages = await initialize_broadcast_messages.system(
                InitializeBroadcastMessagesDto(task_id, messages)
            )
        message_by_user_id = {message.user_id: message for message in messages}
        pending_users = _pending_broadcast_users(users, messages)

        total_users = len(users)
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        total_retry_time = 0.0
        semaphore = asyncio.Semaphore(BATCH_SIZE_20)

        logger.info(
            f"Started sending broadcast '{task_id}', total users: {total_users}, "
            f"pending users: {len(pending_users)}"
        )

        was_canceled = False

        async def send_one(user: UserDto) -> tuple:
            nonlocal total_retry_time
            status = BroadcastMessageStatus.FAILED
            msg_id = None
            retry_time_for_user = 0.0

            while True:
                try:
                    async with semaphore:
                        tg_message = await notifier.notify_user(
                            user,
                            payload=current_broadcast.payload,
                        )

                    if tg_message:
                        status = BroadcastMessageStatus.SENT
                        msg_id = tg_message.message_id

                    return user.id, user.telegram_id, status, msg_id, retry_time_for_user

                except TelegramRetryAfter as error:
                    wait_time = error.retry_after + BATCH_DELAY
                    logger.warning(f"Flood wait {error.retry_after}s for user {user.log}")
                    if retry_time_for_user + wait_time > BROADCAST_MAX_RETRY_WAIT_SECONDS:
                        logger.error(
                            f"Broadcast retry budget exhausted for {user.log}; "
                            f"limit={BROADCAST_MAX_RETRY_WAIT_SECONDS}s"
                        )
                        return user.id, user.telegram_id, status, msg_id, retry_time_for_user
                    await asyncio.sleep(wait_time)
                    retry_time_for_user += wait_time
                    total_retry_time += wait_time
                except Exception:
                    logger.exception(f"Failed to send to {user.log}")
                    return user.id, user.telegram_id, status, msg_id, retry_time_for_user

        for i, batch in enumerate(chunked(pending_users, BATCH_SIZE_20), start=1):
            # Check cancellation every batch (a single cheap SELECT) so a cancel request
            # stops sending promptly instead of after up to ~100 more messages.
            current = await broadcast_dao.get_by_task_id(task_id)
            if not current or current.status == BroadcastStatus.CANCELED:
                logger.info(f"Broadcast '{task_id}' was canceled")
                was_canceled = True
                break

            tasks = [asyncio.create_task(send_one(user)) for user in batch]
            results = await asyncio.gather(*tasks)

            updates = UpdateBroadcastMessageStatusDto(
                task_id=task_id,
                messages=[
                    BroadcastMessageDto(
                        id=message_by_user_id[uid].id,
                        user_id=uid,
                        user_telegram_id=tg_id,
                        status=status,
                        message_id=msg_id,
                    )
                    for uid, tg_id, status, msg_id, _ in results
                ],
            )

            await update_broadcast_message_status.system(updates)

            sent_count = sum(
                1 for _, _, status, _, _ in results if status == BroadcastMessageStatus.SENT
            )
            failed_count = len(results) - sent_count
            batch_retry_time = sum(r[4] for r in results)

            logger.info(
                f"Batch {i}: sent={sent_count}, failed={failed_count}, "
                f"retry_time={batch_retry_time:.2f}s"
            )

        total_elapsed = loop.time() - start_time
        final_status = BroadcastStatus.CANCELED if was_canceled else BroadcastStatus.COMPLETED
        await finish_broadcast.system(FinishBroadcastDto(task_id, final_status))
        logger.success(
            f"Broadcast '{task_id}' finished in {total_elapsed:.2f}s "
            f"with total retry time {total_retry_time:.2f}s"
        )

    except Exception:
        logger.exception(f"Broadcast '{task_id}' failed with an unexpected error")
        await finish_broadcast.system(FinishBroadcastDto(task_id, BroadcastStatus.ERROR))
        return


async def _run_send_broadcast_task(
    broadcast: BroadcastDto,
    plan_id: Optional[int],
    broadcast_dao: FromDishka[BroadcastDao],
    get_broadcast_audience_users: FromDishka[GetBroadcastAudienceUsers],
    initialize_broadcast_messages: FromDishka[InitializeBroadcastMessages],
    update_broadcast_message_status: FromDishka[UpdateBroadcastMessageStatus],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
    broadcast_execution_lock: FromDishka[BroadcastExecutionLock],
) -> None:
    # Blocking is intentional: returning successfully while another invocation
    # owns the task would ACK and lose this replay. Once acquired, re-read all
    # persisted state before deciding whether any user still needs a message.
    async with broadcast_execution_lock.hold(broadcast.task_id):
        await _send_broadcast_locked(
            broadcast,
            plan_id,
            broadcast_dao,
            get_broadcast_audience_users,
            initialize_broadcast_messages,
            update_broadcast_message_status,
            finish_broadcast,
            notifier,
        )


@broker.task
@inject(patch_module=True)
async def send_broadcast_task(
    broadcast: BroadcastDto,
    plan_id: Optional[int],
    broadcast_dao: FromDishka[BroadcastDao],
    get_broadcast_audience_users: FromDishka[GetBroadcastAudienceUsers],
    initialize_broadcast_messages: FromDishka[InitializeBroadcastMessages],
    update_broadcast_message_status: FromDishka[UpdateBroadcastMessageStatus],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
    broadcast_execution_lock: FromDishka[BroadcastExecutionLock],
) -> None:
    await _run_send_broadcast_task(
        broadcast,
        plan_id,
        broadcast_dao,
        get_broadcast_audience_users,
        initialize_broadcast_messages,
        update_broadcast_message_status,
        finish_broadcast,
        notifier,
        broadcast_execution_lock,
    )


async def _finalize_broadcast_deletion(
    finish_broadcast: FinishBroadcast,
    notifier: Notifier,
    broadcast: BroadcastDto,
    total: int,
    deleted: int,
) -> None:
    await finish_broadcast.system(
        FinishBroadcastDto(task_id=broadcast.task_id, status=BroadcastStatus.DELETED)
    )
    await notifier.notify_admins(
        MessagePayloadDto(
            i18n_key="ntf-broadcast.deleted-success",
            i18n_kwargs={
                "task_id": str(broadcast.task_id),
                "total_count": total,
                "deleted_count": deleted,
                "failed_count": total - deleted,
            },
            disable_default_markup=False,
        )
    )


async def _delete_broadcast_locked(  # noqa: C901
    broadcast: BroadcastDto,
    bot: Bot,
    broadcast_dao: BroadcastDao,
    bulk_update_broadcast_messages: BulkUpdateBroadcastMessages,
    finish_broadcast: FinishBroadcast,
    notifier: Notifier,
) -> tuple[int, int, int]:
    current_broadcast = await broadcast_dao.get_by_task_id(broadcast.task_id)
    if current_broadcast is None:
        logger.warning(f"Broadcast '{broadcast.task_id}' no longer exists; skipping deletion")
        return 0, 0, 0

    # Never trust the serialized Taskiq payload after waiting for a previous
    # send/delete invocation.  Its message statuses can be arbitrarily stale.
    broadcast = current_broadcast
    broadcast_id = cast(int, broadcast.id)

    if broadcast.status == BroadcastStatus.DELETED:
        total_messages = len(broadcast.messages)
        deleted_count = sum(
            message.status == BroadcastMessageStatus.DELETED for message in broadcast.messages
        )
        logger.info(f"Broadcast '{broadcast_id}' is already deleted; skipping replay")
        return total_messages, deleted_count, total_messages - deleted_count

    if not broadcast.messages:
        logger.warning(f"No messages to delete for broadcast '{broadcast_id}'")
        await _finalize_broadcast_deletion(finish_broadcast, notifier, broadcast, 0, 0)
        return 0, 0, 0

    logger.info(f"Started deleting messages for broadcast '{broadcast_id}'")

    deleted_count = 0
    total_messages = len(broadcast.messages)
    total_retry_time = 0.0

    loop = asyncio.get_running_loop()
    start_time = loop.time()
    semaphore = asyncio.Semaphore(BATCH_SIZE_20)

    async def delete_one(message: BroadcastMessageDto) -> tuple[BroadcastMessageDto, float]:
        nonlocal total_retry_time
        retry_time_for_msg = 0.0

        if (
            message.status not in (BroadcastMessageStatus.SENT, BroadcastMessageStatus.EDITED)
            or not message.message_id
            or not message.user_telegram_id
        ):
            return message, retry_time_for_msg

        user_telegram_id: int = message.user_telegram_id
        while True:
            try:
                async with semaphore:
                    if await bot.delete_message(
                        chat_id=user_telegram_id, message_id=message.message_id
                    ):
                        message.status = BroadcastMessageStatus.DELETED

                return message, retry_time_for_msg

            except TelegramRetryAfter as error:
                wait_time = error.retry_after + BATCH_DELAY
                logger.warning(
                    f"Flood wait {error.retry_after}s for user '{message.user_telegram_id}'"
                )
                await asyncio.sleep(wait_time)
                retry_time_for_msg += wait_time
                total_retry_time += wait_time

            except Exception:
                logger.exception(
                    f"Exception deleting message for user '{message.user_telegram_id}'"
                )
                return message, retry_time_for_msg

    for i, batch in enumerate(chunked(broadcast.messages, BATCH_SIZE_20), start=1):
        tasks = [asyncio.create_task(delete_one(m)) for m in batch]
        results = await asyncio.gather(*tasks)

        updated_messages = [r[0] for r in results]
        batch_retry_time = sum(r[1] for r in results)

        await bulk_update_broadcast_messages.system(updated_messages)

        batch_deleted = sum(
            1 for m in updated_messages if m.status == BroadcastMessageStatus.DELETED
        )
        deleted_count += batch_deleted

        logger.info(f"Batch {i}: deleted={batch_deleted}, retry_time={batch_retry_time:.2f}s")

        if batch_retry_time == 0:
            await asyncio.sleep(BATCH_DELAY)

    total_elapsed = loop.time() - start_time
    logger.success(
        f"Deletion finished for broadcast '{broadcast_id}' "
        f"Total: {total_messages}, Deleted: {deleted_count}, "
        f"Time: {total_elapsed:.2f}s, Retry time: {total_retry_time:.2f}s"
    )

    await _finalize_broadcast_deletion(
        finish_broadcast, notifier, broadcast, total_messages, deleted_count
    )
    return total_messages, deleted_count, total_messages - deleted_count


async def _run_delete_broadcast_task(
    broadcast: BroadcastDto,
    bot: Bot,
    broadcast_dao: BroadcastDao,
    bulk_update_broadcast_messages: BulkUpdateBroadcastMessages,
    finish_broadcast: FinishBroadcast,
    notifier: Notifier,
    broadcast_execution_lock: BroadcastExecutionLock,
) -> tuple[int, int, int]:
    # Use the same task-scoped lock as delivery.  This serializes send/delete
    # and duplicate delete entries, and blocking keeps the current reclaimed
    # stream entry alive until it can safely re-read persisted state.
    async with broadcast_execution_lock.hold(broadcast.task_id):
        return await _delete_broadcast_locked(
            broadcast,
            bot,
            broadcast_dao,
            bulk_update_broadcast_messages,
            finish_broadcast,
            notifier,
        )


@broker.task
@inject(patch_module=True)
async def delete_broadcast_task(
    broadcast: BroadcastDto,
    bot: FromDishka[Bot],
    broadcast_dao: FromDishka[BroadcastDao],
    bulk_update_broadcast_messages: FromDishka[BulkUpdateBroadcastMessages],
    finish_broadcast: FromDishka[FinishBroadcast],
    notifier: FromDishka[Notifier],
    broadcast_execution_lock: FromDishka[BroadcastExecutionLock],
) -> tuple[int, int, int]:
    return await _run_delete_broadcast_task(
        broadcast,
        bot,
        broadcast_dao,
        bulk_update_broadcast_messages,
        finish_broadcast,
        notifier,
        broadcast_execution_lock,
    )


@broker.task(schedule=[{"cron": "0 0 */7 * *"}])
@inject(patch_module=True)
async def delete_broadcasts_task(clear_old_broadcasts: FromDishka[ClearOldBroadcasts]) -> None:
    await clear_old_broadcasts.system()
