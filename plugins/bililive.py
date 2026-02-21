from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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
ROOM_BATCH_STATUS_API_URL = (
    "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
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
    uid: Mapped[int | None] = mapped_column(nullable=True)
    uname: Mapped[str] = mapped_column(String(255), default="")
    is_live: Mapped[bool | None] = mapped_column(nullable=True)
    live_time: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(1024), default="")


@dataclass
class RoomState:
    room_id: int
    uid: int | None
    uname: str
    is_live: bool | None
    live_time: str
    title: str


class RuntimeState:
    def __init__(self) -> None:
        self.refresh_task: asyncio.Task[None] | None = None
        self.room_uid_map: dict[int, int] = {}
        self.room_uname_map: dict[int, str] = {}


runtime = RuntimeState()


@dataclass
class GroupNotification:
    group_id: int
    room_id: int
    uname: str
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
        if value in {0, 2}:
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
    uid_raw = body.get("uid")
    uid = int(uid_raw) if isinstance(uid_raw, (int, str)) and str(uid_raw).isdigit() else None

    live_time = str(body.get("live_time", ""))
    if live_time == "0000-00-00 00:00:00":
        live_time = ""
    uname = str(body.get("uname", body.get("anchor_name", "")))
    title = str(body.get("title", body.get("room_title", "")))

    logger.debug(
        f"房间状态请求完成: room_id={room_id}, is_live={is_live}, "
        f"live_time={live_time}, title={title}"
    )

    return RoomState(
        room_id=room_id,
        uid=uid,
        uname=uname,
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


def _fetch_room_states_by_uids_sync(uids: list[int]) -> dict[int, RoomState]:
    if not uids:
        return {}

    query_string = urlencode({"uids[]": uids}, doseq=True)
    request_url = f"{ROOM_BATCH_STATUS_API_URL}?{query_string}"
    logger.debug(f"开始批量请求房间状态: uid_count={len(uids)}, url={request_url}")

    request = Request(url=request_url, headers=ROOM_API_HEADERS)
    with urlopen(request, timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("B站批量状态接口返回不是 dict")

    code = payload.get("code")
    if code not in (None, 0):
        message = payload.get("message", payload.get("msg", ""))
        raise ValueError(f"B站批量状态接口返回异常: code={code}, message={message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        logger.debug("批量状态接口 data 非 dict，返回空结果")
        return {}

    room_states: dict[int, RoomState] = {}
    for item in data.values():
        if not isinstance(item, dict):
            continue

        room_id_raw = item.get("room_id")
        uid_raw = item.get("uid")

        room_id = int(room_id_raw) if isinstance(room_id_raw, (int, str)) and str(room_id_raw).isdigit() else 0
        uid = int(uid_raw) if isinstance(uid_raw, (int, str)) and str(uid_raw).isdigit() else None
        if room_id <= 0:
            continue

        live_value = item.get("live_status", item.get("status"))
        is_live = _to_live_state(live_value)

        live_time_raw = item.get("live_time", "")
        if isinstance(live_time_raw, (int, float)):
            live_time = "" if live_time_raw <= 0 else str(int(live_time_raw))
        else:
            live_time = str(live_time_raw)
            if live_time in {"0", "0000-00-00 00:00:00"}:
                live_time = ""

        title = str(item.get("title", item.get("room_title", "")))
        uname = str(item.get("uname", ""))

        room_states[room_id] = RoomState(
            room_id=room_id,
            uid=uid,
            uname=uname,
            is_live=is_live,
            live_time=live_time,
            title=title,
        )

    logger.debug(f"批量请求房间状态完成: requested_uids={len(uids)}, got_rooms={len(room_states)}")
    return room_states


async def _fetch_room_states_by_uids(uids: list[int]) -> dict[int, RoomState]:
    try:
        return await asyncio.to_thread(_fetch_room_states_by_uids_sync, uids)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
        logger.warning(f"批量拉取房间状态失败: {error}")
        return {}


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
            display_name = runtime.room_uname_map.get(room_id, "未知主播")
            lines.append(f"主播 {display_name}: 状态未知（等待刷新）")
            continue

        display_name = room_status.uname.strip() if room_status.uname else runtime.room_uname_map.get(room_id, "未知主播")
        live_text = "直播中" if room_status.is_live else "未开播"
        line = f"主播 {display_name} | {live_text}"
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
    display_name = notification.uname.strip() if notification.uname else "未知主播"
    line = f"订阅主播 {display_name} 状态变更：{live_text}"
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

    try:
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

        runtime.room_uid_map = {
            room_id: uid
            for room_id, uid in runtime.room_uid_map.items()
            if room_id in room_to_groups
        }
        runtime.room_uname_map = {
            room_id: uname
            for room_id, uname in runtime.room_uname_map.items()
            if room_id in room_to_groups
        }

        resolved_room_uids = {
            room_id: runtime.room_uid_map[room_id]
            for room_id in room_ids
            if room_id in runtime.room_uid_map
        }
        unresolved_room_ids = [room_id for room_id in room_ids if room_id not in resolved_room_uids]

        room_states_by_room_id: dict[int, RoomState] = {}

        if unresolved_room_ids:
            logger.debug(f"存在未解析uid的房间，回退单房间接口: count={len(unresolved_room_ids)}")
            fallback_tasks = [_fetch_room_state(room_id) for room_id in unresolved_room_ids]
            fallback_results = await asyncio.gather(*fallback_tasks)
            for room_state in fallback_results:
                if room_state is None:
                    continue
                room_states_by_room_id[room_state.room_id] = room_state
                if room_state.uid is not None:
                    runtime.room_uid_map[room_state.room_id] = room_state.uid
                    resolved_room_uids[room_state.room_id] = room_state.uid
                if room_state.uname:
                    runtime.room_uname_map[room_state.room_id] = room_state.uname

        batch_uids = sorted(set(resolved_room_uids.values()))
        if batch_uids:
            batch_room_states = await _fetch_room_states_by_uids(batch_uids)
            for room_id, room_state in batch_room_states.items():
                room_states_by_room_id[room_id] = room_state
                if room_state.uid is not None:
                    runtime.room_uid_map[room_id] = room_state.uid
                if room_state.uname:
                    runtime.room_uname_map[room_id] = room_state.uname

        room_states: list[RoomState] = [
            room_states_by_room_id[room_id]
            for room_id in room_ids
            if room_id in room_states_by_room_id
        ]

        notifications: list[GroupNotification] = []

        async with session.begin():
            for room_state in room_states:
                existing = await session.get(RoomStatus, room_state.room_id)
                if existing is None:
                    logger.debug(f"首次写入房间状态: room_id={room_state.room_id}")
                    session.add(
                        RoomStatus(
                            room_id=room_state.room_id,
                            uid=room_state.uid,
                            uname=room_state.uname,
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
                                uname=room_state.uname,
                                is_live=new_live,
                                live_time=room_state.live_time,
                                title=room_state.title,
                            )
                        )

                    existing.uid = room_state.uid
                    existing.uname = room_state.uname
                existing.is_live = room_state.is_live
                existing.live_time = room_state.live_time
                existing.title = room_state.title

                logger.debug(
                    f"房间状态已更新: room_id={room_state.room_id}, "
                    f"is_live={room_state.is_live}, live_time={room_state.live_time}"
                )

        await _push_group_notifications(notifications)
        logger.debug("本次房间状态刷新完成")

    except Exception:
        logger.exception("房间状态刷新过程中发生未捕获异常")
        raise
    finally:
        logger.debug("关闭数据库 Session")
        await session.close()


async def _refresh_loop() -> None:
    logger.info(f"状态刷新循环启动，间隔={STATUS_REFRESH_INTERVAL_SECONDS}秒")
    while True:
        try:
            logger.debug("进入新一轮状态刷新")
            await _refresh_room_states_once()
        except asyncio.CancelledError:
            logger.info("刷新任务被取消")
            raise
        except Exception:
            logger.exception("状态刷新循环中发生异常，稍后重试")
        
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
        room_status = await session.get(RoomStatus, room_id)
        display_name = (
            room_status.uname.strip()
            if room_status is not None and room_status.uname
            else runtime.room_uname_map.get(room_id, "该主播")
        )
        await subscribe_add.finish(f"{display_name} 已在当前群订阅列表中。")

    session.add(GroupSubscription(group_id=group_id, room_id=room_id))
    await session.commit()
    logger.info(f"订阅添加成功: group_id={group_id}, room_id={room_id}")

    room_state = await _fetch_room_state(room_id)
    if room_state is not None and room_state.uname:
        runtime.room_uname_map[room_id] = room_state.uname
        if room_state.uid is not None:
            runtime.room_uid_map[room_id] = room_state.uid
        room_status = await session.get(RoomStatus, room_id)
        if room_status is None:
            session.add(
                RoomStatus(
                    room_id=room_id,
                    uid=room_state.uid,
                    uname=room_state.uname,
                    is_live=room_state.is_live,
                    live_time=room_state.live_time,
                    title=room_state.title,
                )
            )
        else:
            room_status.uid = room_state.uid
            room_status.uname = room_state.uname
            room_status.is_live = room_state.is_live
            room_status.live_time = room_state.live_time
            room_status.title = room_state.title
        await session.commit()
        await subscribe_add.finish(f"已为当前群添加订阅主播：{room_state.uname}")

    await subscribe_add.finish("已为当前群添加订阅主播。")


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
        room_status = await session.get(RoomStatus, room_id)
        display_name = (
            room_status.uname.strip()
            if room_status is not None and room_status.uname
            else runtime.room_uname_map.get(room_id, "该主播")
        )
        await subscribe_remove.finish(f"{display_name} 不在当前群订阅列表中。")

    room_status = await session.get(RoomStatus, room_id)
    display_name = (
        room_status.uname.strip()
        if room_status is not None and room_status.uname
        else runtime.room_uname_map.get(room_id, "该主播")
    )

    await session.execute(
        delete(GroupSubscription).where(
            GroupSubscription.group_id == group_id,
            GroupSubscription.room_id == room_id,
        )
    )
    await session.commit()
    logger.info(f"订阅删除成功: group_id={group_id}, room_id={room_id}")

    await subscribe_remove.finish(f"已为当前群删除订阅主播：{display_name}")


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
