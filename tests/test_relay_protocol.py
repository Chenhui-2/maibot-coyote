# -*- coding: utf-8 -*-
"""中继协议对齐测试：逐条对照官方 dungeonlab-open/dglab-websocket-server 的 v4-server.ts。

这不是"能连上就行"的测试，而是把官方实现的每条硬性约定钉住：

    1) 业务探活：{"type":"ping"} 必须回 {"type":"pong","ts":...}（App 靠这个判活）
    2) 心跳帧：{"type":"heartbeat"}，**不带 clientId**（官方就是这样）
    3) 握手顺序：hello（自己的 id）→ controller_attached（控制方 id）
    4) 断开可诊断：记录 close_code / close_reason / 异常
    5) 服务端停止：先发 controller_disconnected，再 close(4000)

第 1 条是"总是自动断连"的直接原因：不回 pong，App 判定连接已死就断开重连。

运行：python tests/test_relay_protocol.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    from websockets.client import connect as ws_connect

from coyote.relay import (
    CLOSE_CONTROLLER_DISCONNECTED,
    HEARTBEAT_S,
    CoyoteRelay,
)

PORT = 19993


def ok(message: str) -> None:
    print(f"✓ {message}")


async def recv_json(ws, timeout: float = 2.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    relay = CoyoteRelay(host="127.0.0.1", port=PORT)
    await relay.start()

    # ---- 3. 握手顺序 ----
    app = await ws_connect(f"ws://127.0.0.1:{PORT}?tid={relay.client_id}")
    hello = await recv_json(app)
    assert hello["type"] == "hello", f"首个帧必须是 hello，实际 {hello}"
    assert hello["clientId"] and hello["clientId"] != relay.client_id, "hello 应返回被控方自己的 id"
    attached = await recv_json(app)
    assert attached["type"] == "controller_attached", f"第二个帧应是 controller_attached，实际 {attached}"
    assert attached["clientId"] == relay.client_id, "controller_attached 的 clientId 必须是控制方 id"
    ok("握手顺序：hello（被控方 id）→ controller_attached（控制方 id）")

    # ---- 1. 业务探活 ping → pong ----
    await app.send(json.dumps({"type": "ping"}))
    pong = await recv_json(app)
    assert pong["type"] == "pong", f"收到 ping 必须回 pong，实际 {pong}"
    assert isinstance(pong.get("ts"), int) and pong["ts"] > 0, "pong 必须带毫秒时间戳 ts"
    ok(f"业务探活：ping → pong（ts={pong['ts']}）")

    # pong 帧被静默消费，不会回环
    await app.send(json.dumps({"type": "pong", "ts": 1}))
    await app.send(json.dumps({"type": "message", "data": {"t": "req", "reqId": "x"}}))
    await asyncio.sleep(0.15)
    ok("pong 帧被静默消费（不会造成回环）")

    # ---- 2. 心跳帧格式 ----
    async def push_heartbeat() -> None:
        # 直接调内部方法，避免等 30 秒；必须用中继持有的**服务端**连接发
        frame = json.dumps({"type": "heartbeat"})
        for server_ws in list(relay.targets.values()):
            await relay._safe_send(server_ws, frame, "t")

    await push_heartbeat()
    hb = await recv_json(app)
    assert hb == {"type": "heartbeat"}, f"心跳帧必须就是 {{'type':'heartbeat'}}，实际 {hb}"
    assert "clientId" not in hb, "官方心跳帧不带 clientId，不要自作聪明加字段"
    ok(f"心跳帧格式正确：{hb}（间隔 {HEARTBEAT_S}s）")

    # ---- 4. 断开可诊断 ----
    await app.close()
    await asyncio.sleep(0.25)
    assert relay.disconnect_count == 1, f"断开次数应记为 1，实际 {relay.disconnect_count}"
    last = relay.last_disconnect
    assert last is not None, "没有记录断连信息"
    assert "close_code" in last and "at" in last, f"断连信息不完整: {last}"
    ok(f"断连已记录：close_code={last['close_code']} at={last['at']}")

    # ---- 5. 服务端停止时通知 App ----
    app2 = await ws_connect(f"ws://127.0.0.1:{PORT}?tid={relay.client_id}")
    await recv_json(app2)  # hello
    await recv_json(app2)  # controller_attached

    stop_task = asyncio.create_task(relay.stop())
    notice = await recv_json(app2)
    assert notice["type"] == "controller_disconnected", f"停止时应先发 controller_disconnected，实际 {notice}"
    assert notice["clientId"] == relay.client_id
    ok("停止前通知：controller_disconnected（App 会显示控制方断开而非网络故障）")

    try:
        await asyncio.wait_for(app2.recv(), timeout=2.0)
    except Exception:
        pass
    code = getattr(app2, "close_code", None)
    assert code == CLOSE_CONTROLLER_DISCONNECTED, f"关闭码应为 4000，实际 {code}"
    ok(f"关闭码正确：{code}（官方 CLOSE_CONTROLLER_DISCONNECTED）")
    await stop_task

    # ---- 错误分支：tid 不存在 ----
    relay2 = CoyoteRelay(host="127.0.0.1", port=PORT + 1)
    await relay2.start()
    bad = await ws_connect(f"ws://127.0.0.1:{PORT + 1}?tid=deadbeef")
    err = await recv_json(bad)
    assert err["type"] == "error" and err["code"] == "controller_not_found", f"应回 controller_not_found，实际 {err}"
    # 等客户端真正处理完关闭帧，否则 close_code 还是 None
    try:
        await asyncio.wait_for(bad.recv(), timeout=2.0)
    except Exception:
        pass
    code = getattr(bad, "close_code", None)
    assert code == 4001, f"关闭码应为 4001，实际 {code}"
    ok("错误分支：未知 tid → controller_not_found + close(4001)")
    await relay2.stop()

    # ---- 安全：无 tid 的连接必须被拒绝 ----
    # 没有 tid 就不是合法的被控方。放过去的话，任何人只要连上端口就能把
    # device.op 下发给已配对的设备，等于未授权控制。
    relay3 = CoyoteRelay(host="127.0.0.1", port=PORT + 2)
    await relay3.start()
    intruder = await ws_connect(f"ws://127.0.0.1:{PORT + 2}")
    err = await recv_json(intruder)
    assert err["type"] == "error" and err["code"] == "bad_request", f"无 tid 应被拒绝，实际 {err}"
    try:
        await asyncio.wait_for(intruder.recv(), timeout=2.0)
    except Exception:
        pass
    code = getattr(intruder, "close_code", None)
    assert code == 4003, f"关闭码应为 4003（spectator denied），实际 {code}"
    ok("安全：无 tid 的连接被拒绝（close 4003），无法下发任何指令")
    await relay3.stop()

    # 显式开启旁观时也必须只读
    relay4 = CoyoteRelay(host="127.0.0.1", port=PORT + 3, allow_spectator=True)
    await relay4.start()
    app3 = await ws_connect(f"ws://127.0.0.1:{PORT + 3}?tid={relay4.client_id}")
    await recv_json(app3)  # hello
    await recv_json(app3)  # controller_attached
    spectator = await ws_connect(f"ws://127.0.0.1:{PORT + 3}")
    hello3 = await recv_json(spectator)
    assert hello3["type"] == "hello"
    await spectator.send(json.dumps({
        "type": "message",
        "data": {"t": "req", "reqId": "hack", "m": "device.op",
                 "data": {"s": "x", "c": 0, "t": 7, "v": 0}},
    }))
    await asyncio.sleep(0.3)
    # App 侧不应该收到任何东西
    try:
        leaked = await asyncio.wait_for(app3.recv(), timeout=0.6)
        raise AssertionError(f"旁观者竟然把指令转发给 App 了：{leaked}")
    except asyncio.TimeoutError:
        pass
    ok("安全：旁观模式（显式开启）严格只读，指令不会转发给 App")
    await spectator.close()
    await app3.close()
    await relay4.stop()

    print("\n中继协议对齐通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
