# -*- coding: utf-8 -*-
"""设备层：把上层意图翻译成 V4 RPC，并在本地做安全约束。

V4 RPC（与 dglab-kit / dglab-websocket-server 对齐）：
    控制方 → 中继 → App：
    {"t":"req","reqId":"1","m":"device.op","data":{"s":slotId,"c":0,"t":3,"v":10}}
    动作类型 t：0=波形帧 3=相对增减强度 4=临时强度(到期归零) 7=绝对强度(仅支持 0)
    清理：m="device.op.clear"，data 可为 {"s":slotId,"c":channel} 或省略
    设备：m="devices.get"
    App 上行：{"t":"ev","ev":"devices.snapshot"|"slots.patch"|"custom.action"} / {"t":"resp",...}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .waves import FRAME_MS, build_frames, chunk_frames

logger = logging.getLogger("coyote.device")

CHANNEL_INDEX = {"A": 0, "B": 1}
ACT_PULSE = 0
ACT_ADD = 3
ACT_TEMP = 4
ACT_SET = 7


def clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def normalize_channel(value: Any, fallback: str = "A") -> str | None:
    if value is None or value == "":
        return fallback
    text = str(value).strip().upper()
    if text in ("A", "1"):
        return "A"
    if text in ("B", "2"):
        return "B"
    if text in ("BOTH", "AB", "ALL", "双通道", "全部"):
        return "BOTH"
    return None


class DeviceController:
    def __init__(self, config: dict, relay: Any, log: logging.Logger | None = None) -> None:
        self.cfg = config
        self.relay = relay
        # 同 relay：Runner 里 root logger 是 WARNING，stdlib logger 的 info 会被挡掉，
        # 所以由插件把 ctx.logger 传进来。
        self._log = log or logger
        self.slot_id: str | None = None
        self.devices: list[dict] = []
        self.props: dict = {}
        self.strength = {"A": 0, "B": 0}
        self.armed = not config.get("require_arm", True)
        self.overheated = False
        self.last_command_at = 0.0
        self._req_id = 0
        self._idle_task: asyncio.Task | None = None
        # 临时强度（boost）：通道 → 回滚任务
        self._boosts: dict[str, asyncio.Task] = {}
        self._boost_info: dict[str, dict] = {}

        relay.on("attached", self._on_attached)
        relay.on("detached", self._on_detached)
        relay.on("message", self._on_message)

    # ---------- 状态 ----------
    @property
    def paired(self) -> bool:
        return self.relay.paired

    @property
    def ready(self) -> bool:
        return self.paired and bool(self.slot_id)

    def status(self) -> dict:
        return {
            "paired": self.paired,
            "slot_id": self.slot_id,
            "devices": [
                {"slot_id": d.get("slotId"), "name": d.get("name"), "type": d.get("type")}
                for d in self.devices
            ],
            "strength": dict(self.strength),
            "armed": self.armed,
            "boosts": self.active_boosts(),
            "limits": {
                "max_strength": self.cfg["max_strength"],
                "step_limit": self.cfg["step_limit"],
                "max_pulse_ms": self.cfg["max_pulse_ms"],
                "max_wave_intensity": self.cfg["max_wave_intensity"],
                "max_boost_ms": self.cfg.get("max_boost_ms", 60000),
            },
            "ws_url": self.relay.pair_ws_url,
            # 地址信息：IP 会因 DHCP 续租 / 切网而变，变了旧二维码就废了
            "relay": {
                "ws_url": self.relay.pair_ws_url,
                "address": self.relay.address,
                "address_changed": self.relay.address_changed,
                "last_address_change": self.relay.last_address_change,
                "candidates": self.relay.candidate_urls(),
            },
        }

    # ---------- 事件 ----------
    async def _on_attached(self, _target_id: str) -> None:
        try:
            await self.request_devices()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("拉取设备失败: %s", exc)

    async def _on_detached(self, _target_id: str) -> None:
        self.strength = {"A": 0, "B": 0}
        self.slot_id = None
        self.devices = []
        self.armed = not self.cfg.get("require_arm", True)
        self._log.warning("被控方断开，本地强度已归零")

    async def _on_message(self, data: dict, _target_id: str) -> None:
        if not isinstance(data, dict):
            return
        if data.get("t") == "resp":
            if data.get("error"):
                self._log.warning("RPC 失败: %s", data.get("error"))
            return
        if data.get("t") != "ev":
            return

        event = data.get("ev")
        if event == "devices.snapshot":
            self._set_devices(data.get("devices") or [])
        elif event == "devices.patch":
            removed = set(data.get("removed") or [])
            kept = [d for d in self.devices if d.get("slotId") not in removed]
            self._set_devices(kept + list(data.get("added") or []))
        elif event == "slots.patch":
            for slot in data.get("slots") or []:
                if slot.get("slotId") and slot.get("slotId") != self.slot_id:
                    continue
                self.props.update(slot.get("props") or {})
                self._sync_state(slot.get("slotState") or {})
        elif event == "custom.action":
            self._log.info("App 反馈按钮: %s", data.get("action"))
            if data.get("action") in ("stop", 0):
                await self.emergency_stop("app button")

    def _set_devices(self, devices: list[dict]) -> None:
        self.devices = [d for d in devices if d]
        first = self.devices[0] if self.devices else None
        if first and first.get("slotId"):
            if self.slot_id != first["slotId"]:
                self.slot_id = first["slotId"]
                self._log.info("设备就绪: %s", first.get("name") or first.get("type") or self.slot_id)
        elif not self.devices:
            self.slot_id = None

    def _sync_state(self, state: dict) -> None:
        """从 App 上报里尽量同步真实强度；各版本字段名不一，做容错匹配。"""

        def walk(obj: dict, out: dict) -> None:
            for key, value in (obj or {}).items():
                low = str(key).lower()
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "overheat" in low or "over_heat" in low:
                        out["overheat"] = value > 0
                    elif low in ("a", "strengtha", "strength_a"):
                        out["A"] = value
                    elif low in ("b", "strengthb", "strength_b"):
                        out["B"] = value
                    elif low in ("strength", "intensity"):
                        out[self.cfg.get("default_channel", "A")] = value
                elif isinstance(value, dict):
                    walk(value, out)

        parsed: dict = {}
        walk(state, parsed)
        if "overheat" in parsed:
            self.overheated = bool(parsed["overheat"])
        for channel in ("A", "B"):
            if channel in parsed:
                self.strength[channel] = int(clamp(parsed[channel], 0, 200))

    # ---------- 配对 ----------
    async def request_devices(self) -> list[dict]:
        await self._send("devices.get")
        return self.devices

    # ---------- 解锁 ----------
    async def set_armed(self, value: bool) -> bool:
        """改解锁状态。

        **只允许人类命令路径调用**（``/coyote on`` / ``off`` / ``stop``）。
        插件没有向模型暴露解锁工具，模型侧的强度/波形接口一律先过
        ``_require_armed()``，所以模型无法把自己解锁。
        """
        self.armed = bool(value)
        if not self.armed:
            await self.emergency_stop("disarm")
        return self.armed

    def _require_ready(self) -> None:
        if not self.paired:
            raise RuntimeError("郊狼未连接：请先用手机 DG-LAB App 扫码接入")
        if not self.slot_id:
            raise RuntimeError("已连接但未拿到设备：请确认 App 内已用蓝牙连上郊狼主机")

    def _require_armed(self) -> None:
        if self.cfg.get("require_arm", True) and not self.armed:
            raise RuntimeError("设备处于锁定状态：需要人类先解锁后才能驱动（/coyote on）")

    def _cap(self) -> int:
        if self.overheated and self.cfg.get("overheat_backoff", True):
            return min(self.cfg["max_strength"], 20)
        return self.cfg["max_strength"]

    # ---------- 强度 ----------
    async def set_strength(self, channel: str, value: float, duration_ms: int = 0) -> dict:
        self._require_ready()
        self._require_armed()
        ch = normalize_channel(channel, self.cfg.get("default_channel", "A"))
        if not ch or ch == "BOTH":
            raise ValueError("需要指定单个通道 A 或 B")

        cap = self._cap()
        target = int(clamp(round(float(value or 0)), 0, cap))
        current = self.strength[ch]
        delta = target - current
        limited = abs(delta) > self.cfg["step_limit"]
        if limited:
            delta = (1 if delta > 0 else -1) * self.cfg["step_limit"]
        if delta == 0:
            return {"channel": ch, "strength": current, "capped": target != int(value or 0), "step_limited": limited}

        applied = int(clamp(current + delta, 0, cap))
        if duration_ms:
            # 带时长的设定 = 临时强度：到点回落到**原来的值**，而不是归零。
            return await self._temp_apply(ch, applied, duration_ms, current, requested=value, limited=limited)

        await self._op(ch, ACT_ADD, delta, None)
        self.strength[ch] = applied
        self._touch()
        return {
            "channel": ch,
            "strength": self.strength[ch],
            "requested": value,
            "capped": target != int(value or 0),
            "step_limited": limited,
        }

    async def add_strength(self, channel: str, delta: float, duration_ms: int = 0) -> dict:
        self._require_ready()
        self._require_armed()
        ch = normalize_channel(channel, self.cfg.get("default_channel", "A"))
        if not ch or ch == "BOTH":
            raise ValueError("需要指定单个通道 A 或 B")
        step = self.cfg["step_limit"]
        applied = int(clamp(round(float(delta or 0)), -step, step))
        if applied == 0:
            return {"channel": ch, "strength": self.strength[ch], "changed": 0}
        prev = self.strength[ch]
        next_value = int(clamp(prev + applied, 0, self._cap()))
        real = next_value - prev
        if real == 0:
            return {"channel": ch, "strength": self.strength[ch], "changed": 0, "capped": True}
        if duration_ms:
            result = await self._temp_apply(ch, next_value, duration_ms, prev, requested=next_value)
            return {"channel": ch, "strength": next_value, "changed": real, **result}
        await self._op(ch, ACT_ADD, real, None)
        self.strength[ch] = next_value
        self._touch()
        return {"channel": ch, "strength": next_value, "changed": real}

    async def boost(self, channel: str, delta: float, duration_ms: int) -> dict:
        """临时提高强度：在当前值基础上加 delta，duration 后自动回落到原值。

        用协议原生的 ``t=4 SetTempIntensity``（App 侧到点自动回落），
        同时起一个本地兜底定时器——万一 App 不认 ``d`` 参数，本地也会把强度拉回去，
        避免"插件以为降了、设备还在高位"的状态分叉。
        """
        self._require_ready()
        self._require_armed()
        ch = normalize_channel(channel, self.cfg.get("default_channel", "A"))
        if not ch or ch == "BOTH":
            raise ValueError("需要指定单个通道 A 或 B")

        duration = int(clamp(int(duration_ms or 0), 200, self.cfg.get("max_boost_ms", 60000)))
        step = self.cfg["step_limit"]
        prev = self.strength[ch]
        want = int(clamp(round(float(delta or 0)), -step, step))
        if want == 0:
            raise ValueError("delta 不能为 0（单次变化幅度上限 %d）" % step)
        target = int(clamp(prev + want, 0, self._cap()))
        real = target - prev
        if real == 0:
            return {
                "channel": ch,
                "strength": prev,
                "changed": 0,
                "capped": True,
                "duration_ms": duration,
                "message": "已经在强度上限，无法再提高" if want > 0 else "已经是 0，无法再降低",
            }
        return await self._temp_apply(ch, target, duration, prev, requested=target, limited=abs(want) >= step)

    async def _temp_apply(
        self,
        ch: str,
        target: int,
        duration_ms: int,
        prev: int,
        requested: float | int | None = None,
        limited: bool = False,
    ) -> dict:
        """下发临时强度并安排回滚。"""
        duration = int(clamp(int(duration_ms), 200, self.cfg.get("max_boost_ms", 60000)))
        # 同通道已有临时强度：先取消旧的（回滚交给新的一次，避免两次回滚打架）
        self._cancel_boost(ch)

        await self._op(ch, ACT_TEMP, target, duration)
        self.strength[ch] = target
        self._touch()

        self._boost_info[ch] = {
            "from": prev,
            "to": target,
            "duration_ms": duration,
            "started_at": time.monotonic(),
        }
        task = asyncio.create_task(self._boost_revert(ch, prev, target, duration))
        self._boosts[ch] = task

        self._log.info("临时强度 %s: %d → %d，%dms 后回到 %d", ch, prev, target, duration, prev)
        return {
            "channel": ch,
            "strength": target,
            "previous": prev,
            "changed": target - prev,
            "requested": requested if requested is not None else target,
            "duration_ms": duration,
            "temp": True,
            "capped": False,
            "step_limited": limited,
        }

    async def _boost_revert(self, ch: str, prev: int, target: int, duration_ms: int) -> None:
        try:
            await asyncio.sleep(duration_ms / 1000 + 0.2)
            # 期间人类手动改过强度就不动它（尊重手动操作）
            if self.strength.get(ch) != target:
                self._log.info("临时强度结束，但 %s 通道已被手动改动，跳过回滚", ch)
                return
            delta = prev - target
            if delta:
                await self._op(ch, ACT_ADD, delta, None)
            self.strength[ch] = int(clamp(prev, 0, self._cap()))
            self._log.info("临时强度结束，%s 通道回到 %d", ch, self.strength[ch])
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._log.warning("临时强度回滚失败 %s: %s", ch, exc)
        finally:
            if self._boosts.get(ch) is asyncio.current_task():
                self._boosts.pop(ch, None)
                self._boost_info.pop(ch, None)

    def _cancel_boost(self, ch: str) -> None:
        task = self._boosts.pop(ch, None)
        self._boost_info.pop(ch, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_boosts(self) -> None:
        for ch in list(self._boosts):
            self._cancel_boost(ch)

    def active_boosts(self) -> dict:
        """当前生效的临时强度（剩余毫秒）。"""
        now = time.monotonic()
        result = {}
        for ch, info in self._boost_info.items():
            elapsed_ms = (now - info["started_at"]) * 1000
            remaining = max(0, int(info["duration_ms"] - elapsed_ms))
            result[ch] = {
                "from": info["from"],
                "to": info["to"],
                "remaining_ms": remaining,
                "duration_ms": info["duration_ms"],
            }
        return result

    # ---------- 波形 ----------
    async def pulse(self, channel: str, pattern: str, intensity: float, duration_ms: int) -> dict:
        self._require_ready()
        self._require_armed()
        ch = normalize_channel(channel, self.cfg.get("default_channel", "A"))
        if not ch or ch == "BOTH":
            raise ValueError("需要指定单个通道 A 或 B")

        duration = int(clamp(int(duration_ms or 3000), 200, self.cfg["max_pulse_ms"]))
        amp = int(clamp(int(intensity if intensity is not None else 60), 1, min(self.cfg["max_wave_intensity"], 100)))
        frames = build_frames(pattern, amp, ch, duration)
        # 官方单次下发最多 100 帧（10 秒）。更长的波形分片发：第一片 immediate 立即开播，
        # 后续片追加到缓冲区，避免整段被 App 截断或拒收。
        for index, chunk in enumerate(chunk_frames(frames)):
            await self._op(ch, ACT_PULSE, chunk, len(chunk) * FRAME_MS, immediate=(index == 0))
        self._touch()
        return {"channel": ch, "pattern": pattern, "intensity": amp, "duration_ms": duration, "frames": len(frames)}

    # ---------- 清理 / 急停 ----------
    async def clear(self, channel: str) -> dict:
        ch = normalize_channel(channel, "BOTH")
        targets = ["A", "B"] if ch == "BOTH" else [ch]
        for item in targets:
            self._cancel_boost(item)
            data = {"s": self.slot_id, "c": CHANNEL_INDEX[item]} if self.slot_id else None
            await self._send("device.op.clear", data)
            self.strength[item] = 0
        self._touch()
        return {"cleared": targets}

    async def emergency_stop(self, reason: str = "stop") -> dict:
        # 先掐掉所有临时强度定时器，否则回滚任务会在急停之后又把强度加回去。
        self._cancel_all_boosts()
        for channel in ("A", "B"):
            data = {"s": self.slot_id, "c": CHANNEL_INDEX[channel]} if self.slot_id else None
            try:
                await self._send("device.op.clear", data)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._op(channel, ACT_SET, 0, None)
            except Exception:  # noqa: BLE001
                pass
            self.strength[channel] = 0
        self.armed = not self.cfg.get("require_arm", True)
        self.last_command_at = time.monotonic()
        self._log.warning("急停: %s", reason)
        return {"stopped": True, "strength": dict(self.strength), "armed": self.armed}

    async def dispose(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        await self.emergency_stop("plugin unload")

    # ---------- 内部 ----------
    def _touch(self) -> None:
        self.last_command_at = time.monotonic()
        idle_ms = self.cfg.get("idle_zero_ms", 0)
        if not idle_ms:
            return
        if self._idle_task:
            self._idle_task.cancel()

        async def _watch() -> None:
            try:
                await asyncio.sleep(idle_ms / 1000)
                if self.strength["A"] > 0 or self.strength["B"] > 0:
                    await self.emergency_stop("idle timeout")
            except asyncio.CancelledError:
                pass

        self._idle_task = asyncio.create_task(_watch())

    async def _op(self, channel: str, action: int, value: Any, duration_ms: int | None, immediate: bool = False) -> None:
        data: dict = {"s": self.slot_id, "c": CHANNEL_INDEX[channel], "t": action}
        if duration_ms is not None:
            data["d"] = int(duration_ms)
        if value is not None:
            data["v"] = value
        if immediate:
            data["im"] = True
        await self._send("device.op", data)

    async def _send(self, method: str, data: dict | None = None) -> None:
        if not self.relay.paired:
            raise RuntimeError("郊狼未连接")
        self._req_id += 1
        payload: dict = {"t": "req", "reqId": str(self._req_id), "m": method}
        if data is not None:
            payload["data"] = data
        self.relay.send(payload)
