# -*- coding: utf-8 -*-
"""冒烟测试：不接真机，用假 App 模拟 DG-LAB 被控方。

覆盖：
    1) 中继起得来、能配对、能下发 RPC
    2) 安全层真的夹住了强度上限与单步幅度
    3) 波形帧格式合法
    4) 急停清零并回锁
    5) 插件能被 SDK 正常收集（工具 / 命令 / 首页卡片）
运行：python tests/test_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    from websockets.client import connect as ws_connect

from coyote.device import DeviceController
from coyote.relay import CoyoteRelay

PORT = 19997
CONFIG = {
    "max_strength": 40,
    "step_limit": 15,
    "require_arm": True,
    "default_channel": "A",
    "max_pulse_ms": 10000,
    "max_wave_intensity": 80,
    "idle_zero_ms": 0,
    "overheat_backoff": True,
}

received: list[dict] = []


def ok(message: str) -> None:
    print(f"✓ {message}")


async def main() -> None:
    relay = CoyoteRelay(host="127.0.0.1", port=PORT, public_host="127.0.0.1")
    await relay.start()
    device = DeviceController(CONFIG, relay)
    ok(f"中继已启动 {relay.pair_ws_url}")

    url = f"ws://127.0.0.1:{PORT}?tid={relay.client_id}"
    app = await ws_connect(url)
    hello = json.loads(await app.recv())
    assert hello["type"] == "hello", hello
    ok("App 接入，收到 hello")

    async def pump() -> None:
        async for raw in app:
            frame = json.loads(raw)
            if frame.get("type") == "message":
                received.append(frame["data"])

    pump_task = asyncio.create_task(pump())
    await asyncio.sleep(0.1)

    await app.send(json.dumps({
        "type": "message",
        "data": {"t": "ev", "ev": "devices.snapshot", "devices": [
            {"slotId": "slot-1", "name": "Coyote V3", "type": "coyote"}
        ]},
    }))
    await asyncio.sleep(0.15)

    state = device.status()
    assert state["paired"] is True
    assert state["slot_id"] == "slot-1"
    assert state["armed"] is False, "默认应锁定"
    ok("状态读取正常，默认锁定")

    try:
        await device.set_strength("A", 20)
        raise AssertionError("锁定状态竟然允许驱动")
    except RuntimeError as exc:
        assert "锁定" in str(exc)
    ok("锁定状态拒绝驱动")

    await device.set_armed(True)
    ok("解锁成功")

    result = await device.set_strength("A", 30)
    assert result["strength"] == 15, f"单步限幅失败: {result}"
    assert result["step_limited"] is True

    result = await device.set_strength("A", 30)
    assert result["strength"] == 30

    result = await device.set_strength("A", 200)
    assert result["strength"] == 40, f"上限未夹住: {result}"
    assert result["capped"] is True
    ok("单步限幅与强度上限生效（A=40）")

    result = await device.add_strength("A", -10)
    assert result["strength"] == 30
    ok("相对增减生效")

    received.clear()
    pulse = await device.pulse("B", "心跳", 70, 2000)
    assert pulse["frames"] == 20
    await asyncio.sleep(0.2)
    op = next((d for d in received if d.get("m") == "device.op" and d["data"].get("t") == 0), None)
    assert op is not None, "未下发波形帧"
    assert op["data"]["c"] == 1, "B 通道索引应为 1"
    assert op["data"]["d"] == 2000
    assert len(op["data"]["v"]) == 20
    non_zero = 0
    for frame in op["data"]["v"]:
        assert len(frame) == 16 and all(c in "0123456789ABCDEF" for c in frame), f"帧格式不合法: {frame}"
        assert frame[:8] == "00000000", "B 通道波形不应写入 A 半区"
        if frame[8:] != "00000000":
            non_zero += 1
    assert non_zero >= 10, f"波形应有多数帧带幅度，实际 {non_zero}"
    ok("波形帧格式合法（20 帧 / B 通道）")

    try:
        await device.pulse("A", "不存在的波形", 50, 1000)
        raise AssertionError("未知波形竟然通过了")
    except ValueError:
        pass
    ok("未知波形被拒绝")

    received.clear()
    await device.emergency_stop("test")
    await asyncio.sleep(0.2)
    assert device.strength == {"A": 0, "B": 0}
    assert device.armed is False, "急停应同时回锁"
    clears = [d for d in received if d.get("m") == "device.op.clear"]
    zeros = [d for d in received if d.get("m") == "device.op" and d["data"].get("t") == 7]
    assert len(clears) == 2, f"应清两个通道，实际 {len(clears)}"
    assert len(zeros) == 2, f"应下发两次归零，实际 {len(zeros)}"
    ok("急停清零 + 自动回锁")

    payload = relay.pair_qr_payload
    assert payload.startswith("https://dungeon-lab.cn/s/?v=1&action=socket&url=")
    ok("二维码内容格式正确")

    pump_task.cancel()
    await app.close()
    await device.dispose()
    await relay.stop()


def test_plugin_components() -> None:
    """插件能被 SDK 收集：入口、工具、命令、卡片都在。"""
    import plugin as plugin_module

    instance = plugin_module.create_plugin()
    components = instance.get_components()
    names = {c.get("name") for c in components}
    expected = {
        "coyote_status",
        "coyote_pair",
        "coyote_strength",
        "coyote_boost",
        "coyote_pulse",
        "coyote_waves",
        "coyote_stop",
        "coyote",
        "coyote_home",
    }
    missing = expected - names
    assert not missing, f"缺少组件: {missing}（实际 {sorted(names)}）"
    # 解锁/锁定是人类的专属权限，模型侧不允许有解锁工具
    assert "coyote_arm" not in names, "模型侧出现了 coyote_arm：解锁只能由人类发命令完成"
    assert len(names) == len(components), f"存在同名组件（Host 会覆盖）: {sorted(names)}"
    assert plugin_module.CoyotePlugin.config_model is not None
    defaults = plugin_module.CoyoteConfig()
    assert defaults.safety.max_strength == 40
    ok(f"插件组件收集正常（{len(components)} 个）")


if __name__ == "__main__":
    asyncio.run(main())
    test_plugin_components()
    print("\n全部通过 ✅")
