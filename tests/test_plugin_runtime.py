# -*- coding: utf-8 -*-
"""插件运行时测试：模拟 MaiBot Runner 的注入方式（_set_context + set_plugin_config），
验证插件在 SDK 契约下的真实行为，而不只是"能 import"。

覆盖：
    1) 配置模型 → 内部 dict 的转换与夹取
    2) /coyote 命令解析（pair / status / on / limit / stop / 未知子命令）
    3) 工具在真实配置下的安全约束
    4) on_load / on_unload 的中继生命周期
运行：python tests/test_plugin_runtime.py
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

import plugin as plugin_module

PORT = 19994


def ok(message: str) -> None:
    print(f"✓ {message}")


class FakeSend:
    def __init__(self) -> None:
        self.images: list[tuple[str, str]] = []
        self.texts: list[str] = []

    async def image(self, image_data: str, stream_id: str) -> bool:
        self.images.append((image_data[:24], stream_id))
        return True

    async def text(self, text: str, stream_id: str) -> bool:
        self.texts.append(text)
        return True


class FakeContext:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test.coyote")
        self.send = FakeSend()


async def main() -> None:
    plugin = plugin_module.create_plugin()
    ctx = FakeContext()
    plugin._set_context(ctx)  # 与 Runner 的注入方式一致
    # SDK 要求配置里必须带 [plugin].config_version（Runner 生成 config.toml 时会自动写）
    plugin.set_plugin_config({
        "plugin": {"config_version": "1.0.0"},
        "network": {"host": "127.0.0.1", "port": PORT},
        "safety": {"max_strength": 30, "step_limit": 10, "require_arm": True},
    })

    await plugin.on_load()
    assert plugin.relay is not None, "on_load 没有启动中继"
    ok(f"on_load 起中继 {plugin.relay.pair_ws_url}")

    # 配置夹取：max_strength=30, step_limit=10
    assert plugin._cfg["max_strength"] == 30
    assert plugin._cfg["step_limit"] == 10
    ok("配置模型 → 内部 dict 转换正确")

    # 未连接时的状态
    success, text, intercept = await plugin.handle_coyote(plain_text="/coyote status", stream_id="s1")
    assert success and "未连接" in text and intercept is True
    # 关键：命令必须主动回信。MaiBot 不会把 return 的文本发出去，只写日志。
    assert ctx.send.texts, "命令没有回信！必须调用 ctx.send.text"
    assert ctx.send.texts[-1] == text, "回信内容和返回文本不一致"
    ok("命令解析：/coyote status（且真的发出去了）")

    # 配对：每个候选地址发一张二维码，都发到当前消息流
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote pair", stream_id="s1")
    candidates = plugin.relay.candidate_urls()
    assert success and len(ctx.send.images) == len(candidates) >= 1, (
        f"应每个候选地址发一张码，候选 {len(candidates)}，实发 {len(ctx.send.images)}"
    )
    assert all(sid == "s1" for _, sid in ctx.send.images), "图片没发到当前消息流"
    assert all("ws://" in c["url"] for c in candidates)
    assert all(c["url"] in text for c in candidates), f"文字里应列出全部地址，实际 {text}"
    ok(f"命令解析：/coyote pair → {len(candidates)} 张码 + 全部地址")

    # 只支持局域网：候选地址里不允许出现虚拟局域网 / 公网地址
    assert all(c["kind"] in ("lan", "manual") for c in candidates), (
        f"出现了非局域网候选：{[c for c in candidates if c['kind'] not in ('lan', 'manual')]}"
    )
    ok("候选地址只有局域网（不含虚拟局域网 / 公网）")

    # 中文别名
    success, text, _ = await plugin.handle_coyote(plain_text="/郊狼 状态", stream_id="s1")
    assert success and "未连接" in text
    ok("命令别名：/郊狼 状态")

    # 假 App 接入
    app = await ws_connect(f"ws://127.0.0.1:{PORT}?tid={plugin.relay.client_id}")
    hello = json.loads(await app.recv())
    assert hello["type"] == "hello"
    # 收集中继下发的所有帧，供后面检查 device.op
    received: list[dict] = []

    async def collect() -> None:
        try:
            async for raw in app:
                try:
                    received.append(json.loads(raw))
                except (TypeError, ValueError):
                    pass
        except Exception:  # noqa: BLE001
            pass

    collector = asyncio.create_task(collect())
    await app.send(json.dumps({
        "type": "message",
        "data": {"t": "ev", "ev": "devices.snapshot", "devices": [
            {"slotId": "slot-9", "name": "Coyote V3", "type": "coyote"}
        ]},
    }))
    await asyncio.sleep(0.2)
    assert plugin.device is not None and plugin.device.slot_id == "slot-9"
    ok("App 接入并上报设备快照")

    # 锁定状态下工具拒绝
    try:
        await plugin.coyote_strength(channel="A", value=20)
        raise AssertionError("锁定状态竟然允许驱动")
    except RuntimeError as exc:
        assert "锁定" in str(exc)
    ok("工具在锁定状态拒绝驱动")

    # 解锁
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote on", stream_id="s1")
    assert success and "已解锁" in text
    ok("命令解析：/coyote on")

    # 单步限幅（step_limit=10）：0 → 50 只能到 10
    result = await plugin.coyote_strength(channel="A", value=50)
    assert result["strength"] == 10, f"单步限幅失败: {result}"
    assert result["step_limited"] is True
    ok("工具：单步限幅 10 生效")

    # 改上限：/coyote limit 20 → 再调只能到 20
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote limit 20", stream_id="s1")
    assert success and "20" in text
    result = await plugin.coyote_strength(channel="A", value=200)
    assert result["strength"] == 20, f"上限未生效: {result}"
    ok("命令解析：/coyote limit 20 生效（A=20）")

    # 波形
    pulse = await plugin.coyote_pulse(channel="B", pattern="心跳", intensity=70, duration_s=2)
    assert pulse["frames"] == 20 and pulse["channel"] == "B"
    ok("工具：波形下发（20 帧 / B 通道）")

    # ---- 临时强度（boost）----
    # 前面把上限压到 20 了，先抬回 40，否则 boost 会被上限夹住
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote limit 40", stream_id="s1")
    assert success and "40" in text
    before = plugin.device.strength["A"]
    received.clear()
    result = await plugin.coyote_boost(channel="A", delta=5, duration_s=1)
    await asyncio.sleep(0.2)  # 等收帧
    assert result["temp"] is True, f"boost 应标记为临时强度: {result}"
    assert result["strength"] == before + 5, f"应从 {before} 提到 {before + 5}，实际 {result['strength']}"
    assert result["previous"] == before
    # 中继下发的形态是 {"type":"message","data":{"t":"req","m":"device.op","data":{...}}}
    ops = [
        f["data"]["data"]
        for f in received
        if isinstance(f.get("data"), dict)
        and isinstance(f["data"].get("data"), dict)
        and f["data"]["data"].get("t") == 4
    ]
    assert ops, f"boost 必须下发 t=4 SetTempIntensity（协议原生的临时强度），收到 {received}"
    assert ops[0]["v"] == before + 5 and ops[0]["d"] == 1000, f"t=4 参数不对: {ops[0]}"
    ok(f"临时强度：下发 t=4 v={before + 5} d=1000ms（{before} → {before + 5}）")

    status = plugin.device.status()
    assert "A" in status["boosts"], "status 里应能看到生效中的临时强度"
    assert status["boosts"]["A"]["remaining_ms"] > 0
    ok(f"临时强度在状态里可见（剩 {status['boosts']['A']['remaining_ms']}ms）")

    # 到点必须真的回落（不只是改本地状态）
    received.clear()
    await asyncio.sleep(1.5)
    assert plugin.device.strength["A"] == before, f"到点应回到 {before}，实际 {plugin.device.strength['A']}"
    reverts = [
        f["data"]["data"]
        for f in received
        if isinstance(f.get("data"), dict)
        and isinstance(f["data"].get("data"), dict)
        and f["data"]["data"].get("t") == 3
    ]
    assert reverts and reverts[0]["v"] == -5, f"回滚必须真的下发 t=3（v=-5），实际 {reverts}"
    assert not plugin.device.status()["boosts"], "回滚后不应再有生效中的临时强度"
    ok("临时强度到点真的下发回滚（不是只改本地状态）")

    # 急停要能取消临时强度，否则回滚任务会在急停后把强度加回去
    plugin.device.strength["A"] = before
    await plugin.coyote_boost(channel="A", delta=5, duration_s=2)
    assert plugin.device.strength["A"] == before + 5
    await plugin.device.emergency_stop("test")
    assert not plugin.device.status()["boosts"], "急停应取消临时强度定时器"
    await asyncio.sleep(2.3)
    assert plugin.device.strength["A"] == 0, f"急停后回滚任务不应再改强度，实际 {plugin.device.strength['A']}"
    ok("急停取消临时强度，回滚任务不会在急停后把强度加回去")

    # 恢复解锁状态，继续后面的用例
    await plugin.device.set_armed(True)

    # 命令形式的临时强度：通道可省略
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote boost 5 1", stream_id="s1")
    assert success and "临时强度" in text and "1 秒后自动回到" in text, text
    assert ctx.send.texts[-1] == text, "命令回复必须真的发出去"
    ok(f"命令解析：/coyote boost 5 1（省略通道）→ {text}")
    await asyncio.sleep(1.4)

    success, text, _ = await plugin.handle_coyote(plain_text="/coyote boost A 5", stream_id="s1")
    assert success and "参数不对" in text, text
    ok("命令解析：/coyote boost 缺秒数 → 参数提示")

    # 未知波形
    try:
        await plugin.coyote_pulse(channel="A", pattern="没有这个波形")
        raise AssertionError("未知波形竟然通过了")
    except ValueError:
        pass
    ok("工具：未知波形被拒绝")

    # 急停
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote stop", stream_id="s1")
    assert success and plugin.device is not None
    assert plugin.device.strength == {"A": 0, "B": 0}
    assert plugin.device.armed is False
    ok("命令解析：/coyote stop 清零并回锁")

    # 未知子命令
    success, text, _ = await plugin.handle_coyote(plain_text="/coyote 乱七八糟", stream_id="s1")
    assert success and "用法" in text
    ok("命令解析：未知子命令给用法提示")

    await app.close()

    # 卸载：中继必须停掉
    await plugin.on_unload()
    assert plugin.relay is None
    ok("on_unload 停止中继")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
    print("\n运行时行为全部通过 ✅")
