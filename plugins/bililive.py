from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from math import log
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nonebot import get_bots, get_driver, logger, on_command, on_notice, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Event, PokeNotifyEvent
from nonebot.params import CommandArg
from nonebot_plugin_orm import Model, async_scoped_session, get_session
from sqlalchemy import String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

require("nonebot_plugin_orm")

STATUS_REFRESH_INTERVAL_SECONDS = 30
ROOM_API_URL = (
    "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
)
ROOM_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


driver = get_driver()


class GroupSubscription(Model):
    group_id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(primary_key=True)


class RoomStatus(Model):
    room_id: Mapped[int] = mapped_column(primary_key=True)
    is_live: Mapped[bool | None] = mapped_column(nullable=True)
    live_time: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(1024), default="")


@dataclass
class RoomState:
    room_id: int
    is_live: bool | None
    live_time: str
    title: str


class RuntimeState:
    refresh_task: asyncio.Task[None] | None = None


runtime = RuntimeState()


@dataclass
class GroupNotification:
    group_id: int
    room_id: int
    is_live: bool
    live_time: str
    title: str


def _extract_api_body(api_response: Any) -> dict[str, Any]:
    if not isinstance(api_response, dict):
        logger.debug("API 响应不是 dict，返回空 body")
        return {}

    data = api_response.get("data")
    if isinstance(data, dict):
        return data

    return api_response


def _to_live_state(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"1", "true", "live", "on"}:
            return True
        if lowered in {"0", "false", "offline", "off"}:
            return False
    return None


def _parse_room_id(text: str) -> int | None:
    stripped = text.strip()
    if not stripped or not stripped.isdigit():
        logger.debug(f"房间号解析失败，原始参数: {text!r}")
        return None
    room_id = int(stripped)
    logger.debug(f"房间号解析成功: {room_id}")
    return room_id


def _fetch_room_state_sync(room_id: int) -> RoomState:
    request_url = ROOM_API_URL.format(room_id=room_id)
    logger.debug(f"开始请求房间状态: room_id={room_id}, url={request_url}")
    request = Request(url=request_url, headers=ROOM_API_HEADERS)
    with urlopen(request, timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0):
            message = payload.get("message", payload.get("msg", ""))
            raise ValueError(f"B站接口返回异常: code={code}, message={message}")

    body = _extract_api_body(payload)
    live_value = body.get("live_status", body.get("status"))
    is_live = _to_live_state(live_value)

    live_time = str(body.get("live_time", ""))
    if live_time == "0000-00-00 00:00:00":
        live_time = ""
    title = str(body.get("title", body.get("room_title", "")))

    logger.debug(
        f"房间状态请求完成: room_id={room_id}, is_live={is_live}, "
        f"live_time={live_time}, title={title}"
    )

    return RoomState(
        room_id=room_id,
        is_live=is_live,
        live_time=live_time,
        title=title,
    )


async def _fetch_room_state(room_id: int) -> RoomState | None:
    try:
        return await asyncio.to_thread(_fetch_room_state_sync, room_id)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
        logger.warning(f"拉取房间 {room_id} 状态失败: {error}")
        return None


async def _get_group_room_ids(session: async_scoped_session, group_id: int) -> list[int]:
    logger.debug(f"查询群订阅房间列表: group_id={group_id}")
    result = await session.execute(
        select(GroupSubscription.room_id)
        .where(GroupSubscription.group_id == group_id)
        .order_by(GroupSubscription.room_id.asc())
    )
    room_ids = list(result.scalars().all())
    logger.debug(f"群订阅房间列表查询完成: group_id={group_id}, room_ids={room_ids}")
    return room_ids


async def _render_group_status(session: async_scoped_session, group_id: int) -> str:
    logger.debug(f"开始渲染群状态消息: group_id={group_id}")
    room_ids = await _get_group_room_ids(session, group_id)
    if not room_ids:
        logger.debug(f"群无订阅，返回空提示: group_id={group_id}")
        return "当前群没有订阅任何直播房间。"

    result = await session.execute(select(RoomStatus).where(RoomStatus.room_id.in_(room_ids)))
    status_rows = {row.room_id: row for row in result.scalars().all()}

    lines = ["当前群订阅直播状态："]
    for room_id in room_ids:
        room_status = status_rows.get(room_id)
        if room_status is None:
            lines.append(f"房间 {room_id}: 状态未知（等待刷新）")
            continue

        live_text = "直播中" if room_status.is_live else "未开播"
        line = f"房间 {room_id} | {live_text}"
        if room_status.live_time:
            line = f"{line} | 开播时间：{room_status.live_time}"
        if room_status.title:
            line = f"{line} | 标题：{room_status.title}"
        lines.append(line)

    rendered = "\n".join(lines)
    logger.debug(f"群状态消息渲染完成: group_id={group_id}, lines={len(lines)}")
    return rendered


def _render_status_change_message(notification: GroupNotification) -> str:
    live_text = "开播" if notification.is_live else "下播"
    line = f"订阅房间 {notification.room_id} 状态变更：{live_text}"
    if notification.live_time:
        line = f"{line}\n开播时间：{notification.live_time}"
    if notification.title:
        line = f"{line}\n标题：{notification.title}"
    return line


async def _push_group_notifications(notifications: list[GroupNotification]) -> None:
    if not notifications:
        logger.debug("无状态变更通知需要推送")
        return

    logger.debug(f"准备推送状态变更通知: count={len(notifications)}")

    bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
    if not bots:
        logger.warning("状态变更通知发送失败：当前没有可用 OneBot V11 Bot 实例")
        return

    for notification in notifications:
        message = _render_status_change_message(notification)
        delivered = False
        for bot in bots:
            try:
                await bot.send_group_msg(group_id=notification.group_id, message=message)
                delivered = True
                logger.info(
                    f"状态变更通知发送成功: bot={bot.self_id}, "
                    f"group_id={notification.group_id}, room_id={notification.room_id}, "
                    f"is_live={notification.is_live}"
                )
                break
            except Exception as error:  # noqa: BLE001
                logger.debug(
                    f"使用 bot {bot.self_id} 向群 {notification.group_id} 推送失败: {error}"
                )
        if not delivered:
            logger.warning(
                f"状态变更通知发送失败：room_id={notification.room_id}, "
                f"group_id={notification.group_id}"
            )


async def _refresh_room_states_once() -> None:
    logger.debug("开始执行一次房间状态刷新")
    session = get_session()

    async with session.begin():
        logger.debug("查询所有订阅记录")
        subscription_result = await session.execute(
            select(GroupSubscription.group_id, GroupSubscription.room_id)
        )
        subscriptions = list(subscription_result.all())
        logger.debug(f"订阅记录查询完成: count={len(subscriptions)}")

    room_to_groups: dict[int, list[int]] = {}
    for group_id, room_id in subscriptions:
        room_to_groups.setdefault(room_id, []).append(group_id)

        logger.debug(
            f"处理订阅记录: group_id={group_id}, "
            f"room_id={room_id}, "
            f"current_groups={room_to_groups[room_id]}"
        )

    room_ids = sorted(room_to_groups)
    logger.debug(
        f"刷新订阅统计: subscriptions={len(subscriptions)}, "
        f"unique_rooms={len(room_ids)}"
    )

    if not room_ids:
        logger.debug("没有订阅房间，本次刷新结束")
        return

    room_states: list[RoomState] = []
    for room_id in room_ids:
        room_state = await _fetch_room_state(room_id)
        if room_state is None:
            logger.debug(f"房间状态拉取失败并跳过: room_id={room_id}")
            continue
        room_states.append(room_state)

    notifications: list[GroupNotification] = []

    async with session.begin():
        for room_state in room_states:
            existing = await session.get(RoomStatus, room_state.room_id)
            if existing is None:
                logger.debug(f"首次写入房间状态: room_id={room_state.room_id}")
                session.add(
                    RoomStatus(
                        room_id=room_state.room_id,
                        is_live=room_state.is_live,
                        live_time=room_state.live_time,
                        title=room_state.title,
                    )
                )
                continue

            old_live = existing.is_live
            new_live = room_state.is_live

            if (
                old_live is not None
                and new_live is not None
                and old_live != new_live
                and room_state.room_id in room_to_groups
            ):
                logger.info(
                    f"检测到直播状态变化: room_id={room_state.room_id}, old={old_live}, "
                    f"new={new_live}, groups={room_to_groups[room_state.room_id]}"
                )
                for group_id in room_to_groups[room_state.room_id]:
                    notifications.append(
                        GroupNotification(
                            group_id=group_id,
                            room_id=room_state.room_id,
                            is_live=new_live,
                            live_time=room_state.live_time,
                            title=room_state.title,
                        )
                    )

            existing.is_live = room_state.is_live
            existing.live_time = room_state.live_time
            existing.title = room_state.title

            logger.debug(
                f"房间状态已更新: room_id={room_state.room_id}, "
                f"is_live={room_state.is_live}, live_time={room_state.live_time}"
            )

    await _push_group_notifications(notifications)
    logger.debug("本次房间状态刷新完成")


async def _refresh_loop() -> None:
    logger.info(f"状态刷新循环启动，间隔={STATUS_REFRESH_INTERVAL_SECONDS}秒")
    while True:
        logger.debug("进入新一轮状态刷新")
        await _refresh_room_states_once()
        await asyncio.sleep(STATUS_REFRESH_INTERVAL_SECONDS)


@driver.on_startup
async def _on_startup() -> None:
    logger.info("bililive 插件启动中，先执行首次状态刷新")
    await _refresh_room_states_once()
    runtime.refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("bililive 插件已启动（ORM 模式）")


@driver.on_shutdown
async def _on_shutdown() -> None:
    logger.info("bililive 插件准备关闭")
    if runtime.refresh_task is None:
        logger.debug("刷新任务不存在，跳过取消")
        return

    runtime.refresh_task.cancel()
    try:
        await runtime.refresh_task
    except asyncio.CancelledError:
        logger.debug("刷新任务已取消")
        pass
    runtime.refresh_task = None
    logger.info("bililive 插件已关闭")


subscribe_add = on_command("直播订阅添加", priority=5, block=True)
subscribe_remove = on_command("直播订阅删除", priority=5, block=True)
subscribe_list = on_command("直播订阅列表", priority=5, block=True)
poke_status = on_notice(priority=10, block=False)


@subscribe_add.handle()
async def _handle_subscribe_add(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    logger.debug(f"收到订阅添加指令: group_id={event.group_id}, raw_args={args!r}")
    room_id = _parse_room_id(args.extract_plain_text())
    if room_id is None:
        logger.debug("订阅添加参数不合法")
        await subscribe_add.finish("用法：/直播订阅添加 房间号")

    group_id = int(event.group_id)
    existed = await session.get(GroupSubscription, {"group_id": group_id, "room_id": room_id})
    if existed is not None:
        logger.debug(f"订阅添加被忽略，已存在: group_id={group_id}, room_id={room_id}")
        await subscribe_add.finish(f"房间 {room_id} 已在当前群订阅列表中。")

    session.add(GroupSubscription(group_id=group_id, room_id=room_id))
    await session.commit()
    logger.info(f"订阅添加成功: group_id={group_id}, room_id={room_id}")

    await subscribe_add.finish(f"已为当前群添加订阅房间：{room_id}")


@subscribe_remove.handle()
async def _handle_subscribe_remove(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    logger.debug(f"收到订阅删除指令: group_id={event.group_id}, raw_args={args!r}")
    room_id = _parse_room_id(args.extract_plain_text())
    if room_id is None:
        logger.debug("订阅删除参数不合法")
        await subscribe_remove.finish("用法：/直播订阅删除 房间号")

    group_id = int(event.group_id)
    existed = await session.get(GroupSubscription, {"group_id": group_id, "room_id": room_id})
    if existed is None:
        logger.debug(f"订阅删除被忽略，不存在: group_id={group_id}, room_id={room_id}")
        await subscribe_remove.finish(f"房间 {room_id} 不在当前群订阅列表中。")

    await session.execute(
        delete(GroupSubscription).where(
            GroupSubscription.group_id == group_id,
            GroupSubscription.room_id == room_id,
        )
    )
    await session.commit()
    logger.info(f"订阅删除成功: group_id={group_id}, room_id={room_id}")

    await subscribe_remove.finish(f"已为当前群删除订阅房间：{room_id}")


@subscribe_list.handle()
async def _handle_subscribe_list(
    event: GroupMessageEvent,
    session: async_scoped_session,
) -> None:
    group_id = int(event.group_id)
    logger.debug(f"收到订阅列表指令: group_id={group_id}")
    await subscribe_list.finish(await _render_group_status(session, group_id))


@poke_status.handle()
async def _handle_poke_status(bot: Bot, event: Event, session: async_scoped_session) -> None:
    logger.debug(f"收到 notice 事件: type={type(event).__name__}")
    if not isinstance(event, PokeNotifyEvent):
        return

    group_id = getattr(event, "group_id", None)
    if group_id is None:
        logger.debug("拍一拍事件无 group_id，忽略")
        return

    if str(event.target_id) != str(event.self_id):
        logger.debug(
            f"拍一拍目标不是机器人，忽略: target_id={event.target_id}, "
            f"self_id={event.self_id}"
        )
        return

    logger.info(f"触发拍一拍状态查询: group_id={group_id}, bot_id={bot.self_id}")
    async with session.begin():
        message = await _render_group_status(session, int(group_id))
    await bot.send_group_msg(group_id=int(group_id), message=message)
    logger.info(f"拍一拍状态消息发送完成: group_id={group_id}")
