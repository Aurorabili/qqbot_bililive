# bililive

## How to start

1. generate project using `nb create` .
2. create your plugin using `nb plugin create` .
3. writing your plugins under `bililive/plugins` folder.
4. run your bot using `nb run --reload` .

## Documentation

See [Docs](https://nonebot.dev/)

## bililive 插件说明

- 插件文件：`plugins/bililive.py`
- 订阅表与房间状态通过 `nonebot-plugin-orm` 持久化到数据库
- 后台会每 30 秒异步刷新一次所有已订阅房间状态（自动去重房间号）
- 房间状态数据来源：`https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}`
- 主要持久化字段：开播状态（`live_status`）、开播时间（`live_time`）、房间标题（`title`）

### ORM 初始化

0. 在 `.env` 中配置默认数据库（SQLite）：`SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:///./data/bililive.sqlite3`
1. 安装依赖后执行数据库迁移：`nb orm upgrade`
2. 可选检查：`nb orm check`

### 指令

- `/直播订阅添加 <房间号>`：为当前群添加订阅
- `/直播订阅删除 <房间号>`：为当前群删除订阅
- `/直播订阅列表`：查看当前群已订阅房间状态

### 拍一拍触发

当群内成员拍一拍机器人时，机器人会发送当前群订阅房间的直播状态。
