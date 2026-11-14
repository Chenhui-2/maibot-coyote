# -*- coding: utf-8 -*-
"""maibot-coyote —— 麦麦（MaiBot）的郊狼（DG-LAB）控制插件。

做的事：内置 Socket V4 中继 → 手机 DG-LAB App 扫码接入 → 给模型一组工具，
让麦麦能读状态、调强度、打波形、急停。所有下发都先过本地安全层。

**解锁/锁定是人类的专属权限**：只有 `/coyote on` / `/coyote off` / `/coyote stop` 能改解锁状态，
插件不向模型暴露任何解锁工具。模型想动手，只能等人先解锁。

**请在确保无初始强度的情况下启动本插件**：本插件以「设备当前强度为 0」为前提做增量控制，
启动前请确认 App 里 A/B 两个通道都是 0。

仅供成年人在自愿、知情、同意的前提下使用。电极不可跨心脏、不可置于颈部以上。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HomeCard,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# Runner 用 spec_from_file_location(..., submodule_search_locations=[插件目录]) 加载 plugin.py，
# 所以插件本身是个包，相对导入可用；只有被当成普通模块导入时（本地测试）才退回 sys.path 方案。
if __package__:
    from .coyote.device import DeviceController, normalize_channel
    from .coyote.qr import qr_png_base64
    from .coyote.relay import CoyoteRelay
    from .coyote.waves import catalog, reload_waveforms, resolve_pattern
else:  # pragma: no cover - 仅在非包方式导入时走到
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from coyote.device import DeviceController, normalize_channel
    from coyote.qr import qr_png_base64
    from coyote.relay import CoyoteRelay
    from coyote.waves import catalog, reload_waveforms, resolve_pattern

logger = logging.getLogger("coyote.plugin")

MAX_STEP = 40


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件设置"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用郊狼插件")


class NetworkSection(PluginConfigBase):
    """中继网络配置。"""

    __ui_label__ = "中继网络"
    __ui_order__ = 1

    host: str = Field(
        default="0.0.0.0",
        description="中继监听地址",
        json_schema_extra={"label": "监听地址", "placeholder": "0.0.0.0"},
    )
    port: int = Field(
        default=9998,
        description="中继端口，手机 App 需要能访问",
        json_schema_extra={"label": "端口", "step": 1},
    )
    public_host: str = Field(
        default="",
        description="手机访问用的本机 IP，留空自动探测局域网地址",
        json_schema_extra={"label": "对外 IP", "placeholder": "留空自动探测"},
    )
    allow_spectator: bool = Field(
        default=False,
        description="允许无 tid 的第三方连接（只读调试用）。公网暴露时务必保持关闭",
        json_schema_extra={"label": "允许旁观连接"},
    )


class SafetySection(PluginConfigBase):
    """安全约束。默认值偏保守，别为了"更刺激"随便放大。"""

    __ui_label__ = "安全约束"
    __ui_order__ = 2

    max_strength: int = Field(
        default=40,
        description="强度硬上限（1–200），AI 与手动都越不过",
        json_schema_extra={"label": "强度上限", "step": 1},
    )
    step_limit: int = Field(
        default=15,
        description="单次强度变化上限（1–40），防跳变",
        json_schema_extra={"label": "单步上限", "step": 1},
    )
    require_arm: bool = Field(
        default=True,
        description="需要人类先解锁，模型才能驱动设备（解锁只能由人类发命令）",
        json_schema_extra={"label": "需要解锁"},
    )
    default_channel: str = Field(
        default="A",
        description="未指定时的默认通道（A 或 B）",
        json_schema_extra={"label": "默认通道", "placeholder": "A"},
    )
    max_pulse_ms: int = Field(
        default=10000,
        description="单次波形最长时长（毫秒）",
        json_schema_extra={"label": "波形最长时长", "step": 500},
    )
    max_wave_intensity: int = Field(
        default=80,
        description="波形相对幅度上限（1–100）",
        json_schema_extra={"label": "波形幅度上限", "step": 5},
    )
    idle_zero_ms: int = Field(
        default=0,
        description="空闲自动归零（毫秒，0 表示关闭）",
        json_schema_extra={"label": "空闲归零", "step": 1000},
    )
    max_boost_ms: int = Field(
        default=60000,
        description="临时强度（boost）单次最长时长（毫秒，1000–300000）",
        json_schema_extra={"label": "临时强度最长", "step": 5000},
    )
    overheat_backoff: bool = Field(
        default=True,
        description="设备过热时把上限临时压到 20",
        json_schema_extra={"label": "过热降级"},
    )


class CoyoteConfig(PluginConfigBase):
    """郊狼插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    network: NetworkSection = Field(default_factory=NetworkSection)
    safety: SafetySection = Field(default_factory=SafetySection)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _classify_peer(peer: str) -> str:
    """把对端 IP 归类，排查断连时先看这个。

    本插件面向同一局域网使用，非私有网段的来源标成「非局域网」，
    方便人类判断是不是自己把中继接出了局域网。
    """
    if not peer:
        return "来源未知"
    if peer.startswith("127."):
        return "本机"
    if peer.startswith(("192.168.", "10.")):
        return "同一局域网"
    if peer.startswith("172."):
        try:
            second = int(peer.split(".")[1])
        except (IndexError, ValueError):
            return "未知网段"
        return "同一局域网" if 16 <= second <= 31 else "非局域网"
    return "非局域网"


class CoyotePlugin(MaiBotPlugin):
    config_model = CoyoteConfig

    def __init__(self) -> None:
        super().__init__()
        self.relay: CoyoteRelay | None = None
        self.device: DeviceController | None = None
        self._cfg: dict = {}
        self._start_error: str | None = None
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------
    async def on_load(self) -> None:
        self._cfg = self._build_config()
        if not self._cfg["enabled"]:
            self.ctx.logger.info("郊狼插件已在配置中禁用，跳过启动")
            return
        # 安全前提：插件假设设备从 0 开始，增量控制才有意义。
        self.ctx.logger.warning(
            "启动前请确认设备无初始强度（App 里 A/B 通道都为 0）——"
            "本插件以「设备当前强度为 0」为前提做增量控制"
        )
        await self._start()

    async def on_unload(self) -> None:
        await self._stop()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        old = dict(self._cfg)
        self._cfg = self._build_config()
        self.ctx.logger.info("郊狼插件配置已更新: version=%s", version)
        if old.get("host") != self._cfg["host"] or old.get("port") != self._cfg["port"]:
            await self._stop()
            await self._start()
        elif self.device is not None:
            self.device.cfg = self._cfg

    # ---------------- 配置 ----------------
    def _build_config(self) -> dict:
        conf = self.config
        plugin = conf.plugin
        network = conf.network
        safety = conf.safety
        max_strength = _clamp_int(safety.max_strength, 40, 1, 200)
        step_limit = _clamp_int(safety.step_limit, 15, 1, min(MAX_STEP, max_strength))
        channel = normalize_channel(safety.default_channel, "A")
        return {
            "enabled": bool(plugin.enabled),
            "host": str(network.host or "0.0.0.0"),
            "port": _clamp_int(network.port, 9998, 1, 65535),
            "public_host": str(network.public_host or ""),
            "allow_spectator": bool(network.allow_spectator),
            "max_strength": max_strength,
            "step_limit": step_limit,
            "require_arm": bool(safety.require_arm),
            "default_channel": "B" if channel == "B" else "A",
            "max_pulse_ms": _clamp_int(safety.max_pulse_ms, 10000, 200, 60000),
            "max_wave_intensity": _clamp_int(safety.max_wave_intensity, 80, 1, 100),
            "idle_zero_ms": _clamp_int(safety.idle_zero_ms, 0, 0, 600000),
            "max_boost_ms": _clamp_int(safety.max_boost_ms, 60000, 1000, 300000),
            "overheat_backoff": bool(safety.overheat_backoff),
        }

    async def _start(self) -> None:
        self.relay = CoyoteRelay(
            host=self._cfg["host"],
            port=self._cfg["port"],
            public_host=self._cfg["public_host"],
            allow_spectator=self._cfg["allow_spectator"],
            # 必须把插件的 logger 传进去：Runner 里 root logger 是 WARNING，
            # stdlib 的 coyote.relay 的 info 会被挡掉，连接/断开事件一条都看不到。
            log=self.ctx.logger,
        )
        self.device = DeviceController(self._cfg, self.relay, log=self.ctx.logger)
        try:
            await self.relay.start()
            self._start_error = None
            self.ctx.logger.info(
                "郊狼中继已启动 ws=%s 强度上限=%s", self.relay.pair_ws_url, self._cfg["max_strength"]
            )
        except Exception as exc:  # noqa: BLE001
            self._start_error = str(exc)
            self.ctx.logger.error("郊狼中继启动失败（端口 %s）：%s", self._cfg["port"], exc)

    async def _stop(self) -> None:
        if self.device is not None:
            try:
                await self.device.dispose()
            except Exception:  # noqa: BLE001
                pass
            self.device = None
        if self.relay is not None:
            try:
                await self.relay.stop()
            except Exception:  # noqa: BLE001
                pass
            self.relay = None

    # ---------------- 工具方法 ----------------
    def _require(self) -> DeviceController:
        if self._start_error:
            raise RuntimeError(f"中继未启动：{self._start_error}（端口 {self._cfg.get('port')} 可能被占用）")
        if self.device is None:
            raise RuntimeError("郊狼插件未启动")
        return self.device

    def _summary(self) -> str:
        device = self._require()
        state = device.status()
        lines = [
            "已连接手机 App" if state["paired"] else "未连接（需要扫码配对）",
            f"强度：A={state['strength']['A']}  B={state['strength']['B']}  上限={state['limits']['max_strength']}",
            "状态：已解锁（可驱动）" if state["armed"] else "状态：已锁定（需人类先解锁）",
        ]
        # 临时强度还在倒计时的话，让人一眼看到几秒后回落。
        boosts = state.get("boosts") or {}
        if boosts:
            parts = []
            for ch, info in sorted(boosts.items()):
                parts.append(
                    f"{ch} {info['from']}→{info['to']}（剩 {info['remaining_ms'] / 1000:.1f}s 回落）"
                )
            lines.append("临时强度：" + "，".join(parts))

        # 地址：IP 会因 DHCP 续租 / 切 Wi-Fi / 拔网线而变，变了必须一眼看出来。
        relay_state = self.relay.status() if self.relay else {}
        address = relay_state.get("address") or ""
        if address:
            lines.append(f"配对地址：ws://{address}:{self._cfg['port']}")
        if relay_state.get("address_changed"):
            change = relay_state.get("last_address_change") or {}
            if change.get("from") and change.get("to"):
                lines.append(
                    f"⚠️ 本机 IP 已从 {change['from']} 变为 {change['to']}，"
                    "旧二维码已失效，请重新 /coyote pair"
                )
            else:
                lines.append("⚠️ 本机 IP 已变化，旧二维码已失效，请重新 /coyote pair")

        # 连接来源：本插件只支持同一局域网接入，非私有网段的来源会被标出来。
        peers = (relay_state.get("peers") or {})
        if peers:
            for peer in peers.values():
                lines.append(f"连接来源：{peer}（{_classify_peer(peer)}）")
        # 断连诊断：未连接但曾经连上过时，把最后一次断开的原因带出来，
        # 省得为了看 close_code 去翻日志。
        if not state["paired"] and self.relay is not None:
            last = getattr(self.relay, "last_disconnect", None)
            if last:
                detail = f"最近一次断开 {last.get('at')}"
                if last.get("close_code") is not None:
                    detail += f" 关闭码={last['close_code']}"
                if last.get("close_reason"):
                    detail += f" 原因={last['close_reason']}"
                if last.get("error"):
                    detail += f" 异常={last['error']}"
                lines.append(detail)
                lines.append(
                    f"累计断开 {getattr(self.relay, 'disconnect_count', 0)} 次，"
                    "原因见日志「手机 App 已断开」"
                )
        return " / ".join(lines)

    async def _reply(self, stream_id: str, text: str) -> bool:
        """主动把文本发到聊天流。

        MaiBot 的 Command 返回值**不会**被自动发送：``bot._handle_command_processing_result``
        只把它写进日志（"命令处理完成，跳过后续消息处理: xxx"）并存储被拦截的消息。
        所以插件命令必须自己调 ``ctx.send.text``，否则人类侧什么都收不到。
        """
        if not stream_id:
            self.ctx.logger.warning("缺少 stream_id，无法回信：%s", text)
            return False
        try:
            ok = await self.ctx.send.text(text, stream_id)
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("发送命令回复失败")
            return False
        if not ok:
            self.ctx.logger.warning("命令回复发送失败（能力未授权？需要 send.text）：%s", text)
        return bool(ok)

    # ---------------- 工具 ----------------
    @Tool(
        "coyote_status",
        description="查看郊狼（DG-LAB）设备的连接状态、A/B 通道当前强度、解锁状态与安全上限。任何操作前先看这个。",
        parameters=[],
    )
    async def coyote_status(self, **kwargs: Any) -> dict:
        del kwargs
        device = self._require()
        state = device.status()
        return {"success": True, "summary": self._summary(), **state}

    @Tool(
        "coyote_pair",
        description="获取郊狼配对信息（扫码地址）。设备未连接时，让人类用手机 DG-LAB 4.0 App 扫码接入。",
        parameters=[],
    )
    async def coyote_pair(self, **kwargs: Any) -> dict:
        del kwargs
        self._require()
        candidates = self.relay.candidate_urls() if self.relay else []
        return {
            "success": True,
            "connected": bool(self.relay and self.relay.paired),
            "address": self.relay.address if self.relay else "",
            "ws_url": self.relay.pair_ws_url if self.relay else "",
            "qr_payload": self.relay.pair_qr_payload if self.relay else "",
            # 多个地址 = 本机有多块能用的网卡（网线 + Wi-Fi 之类），全都给出来
            "urls": [
                {
                    "label": c["label"],
                    "url": c["url"],
                    "kind": c["kind"],
                    "iface": c.get("iface", ""),
                }
                for c in candidates
            ],
            "note": "本机 IP 会随 DHCP 续租 / 切换网络而变；二维码只对生成时的 IP 有效，连不上就先重新取一次配对信息",
            "steps": "手机与电脑连同一个 Wi-Fi，扫局域网地址那张码；DG-LAB 4.0 App 的 Socket 控制 → 扫描二维码 → App 内蓝牙连郊狼主机",
        }

    @Tool(
        "coyote_strength",
        description=(
            "设置或增减某个通道的强度。value（目标绝对值）与 delta（相对增减）二选一。"
            "单次变化会被限制在配置的单步上限（默认 15）以内，且永不超过强度上限。"
            "给了 duration_s 就是「临时强度」：到点自动回落到原来的值。"
        ),
        parameters=[
            ToolParameterInfo(name="channel", param_type=ToolParamType.STRING, description="通道 A 或 B", required=True),
            ToolParameterInfo(name="value", param_type=ToolParamType.INTEGER, description="目标强度绝对值", required=False),
            ToolParameterInfo(name="delta", param_type=ToolParamType.INTEGER, description="相对增减量，如 -5 或 5", required=False),
            ToolParameterInfo(name="duration_s", param_type=ToolParamType.FLOAT, description="持续秒数，到点回落到原值；不传则保持", required=False),
        ],
    )
    async def coyote_strength(
        self,
        channel: str = "A",
        value: int | None = None,
        delta: int | None = None,
        duration_s: float | None = None,
        **kwargs: Any,
    ) -> dict:
        del kwargs
        device = self._require()
        ch = normalize_channel(channel, self._cfg["default_channel"])
        if not ch or ch == "BOTH":
            raise ValueError('channel 只能是 "A" 或 "B"')
        if value is None and delta is None:
            raise ValueError("value 与 delta 至少给一个")
        duration_ms = int(max(0.0, float(duration_s)) * 1000) if duration_s else 0
        if value is not None:
            result = await device.set_strength(ch, value, duration_ms)
        else:
            result = await device.add_strength(ch, delta or 0, duration_ms)
        return {"success": True, "duration_s": duration_s or 0, **result}

    @Tool(
        "coyote_boost",
        description=(
            "临时提高强度一段时间，到点自动回落到提高之前的值（不需要手动降回去）。\n"
            "适合「来一下、几秒就恢复」的场景，比手动调高再调低更自然。\n"
            "delta 受单步上限约束；时长受配置的 max_boost_ms（默认 60s）约束。\n"
            "同通道重复调用会覆盖上一次的临时强度。急停/锁定会立即取消。"
        ),
        parameters=[
            ToolParameterInfo(name="channel", param_type=ToolParamType.STRING, description="通道 A 或 B", required=True),
            ToolParameterInfo(name="delta", param_type=ToolParamType.INTEGER, description="临时提高多少，如 10（受单步上限约束）", required=True),
            ToolParameterInfo(name="duration_s", param_type=ToolParamType.FLOAT, description="持续多少秒后回落，如 5", required=True),
        ],
    )
    async def coyote_boost(self, channel: str = "A", delta: int = 0, duration_s: float = 5.0, **kwargs: Any) -> dict:
        del kwargs
        device = self._require()
        ch = normalize_channel(channel, self._cfg["default_channel"])
        if not ch or ch == "BOTH":
            raise ValueError('channel 只能是 "A" 或 "B"')
        duration_ms = int(max(0.0, float(duration_s or 0)) * 1000)
        result = await device.boost(ch, delta, duration_ms)
        return {"success": True, **result}

    @Tool(
        "coyote_pulse",
        description=(
            "在某个通道上播放一段波形。波形只调制幅度，实际体感仍受该通道当前强度约束。\n"
            "可用波形：官方经典 24 套（呼吸/潮汐/连击/快速按捏/按捏渐强/心跳节奏/压缩/节奏步伐/"
            "颗粒摩擦/渐变弹跳/波浪涟漪/雨水冲刷/变速敲击/信号灯/挑逗1/挑逗2/挤压/气泡/律动/电波/"
            "舞步/攀登/树荫/脉冲）、程序化 9 种（steady/breathe/heartbeat/tap/tide/climb/grain/press/rain）"
            "以及人类自己加进 waveforms/user.json 的波形。拿不准就先调 coyote_waves 查。"
        ),
        parameters=[
            ToolParameterInfo(name="channel", param_type=ToolParamType.STRING, description="通道 A 或 B", required=True),
            ToolParameterInfo(
                name="pattern",
                param_type=ToolParamType.STRING,
                description="波形名，中英文都行（如 呼吸 / 潮汐 / breathe / tap）",
                required=True,
            ),
            ToolParameterInfo(name="intensity", param_type=ToolParamType.INTEGER, description="波形相对幅度 1-100，默认 60", required=False),
            ToolParameterInfo(name="duration_s", param_type=ToolParamType.FLOAT, description="持续秒数，默认 3；波形比这短会自动循环", required=False),
        ],
    )
    async def coyote_pulse(
        self,
        channel: str = "A",
        pattern: str = "breathe",
        intensity: int = 60,
        duration_s: float = 3,
        **kwargs: Any,
    ) -> dict:
        del kwargs
        device = self._require()
        ch = normalize_channel(channel, self._cfg["default_channel"])
        if not ch or ch == "BOTH":
            raise ValueError('channel 只能是 "A" 或 "B"')
        if not resolve_pattern(pattern):
            raise ValueError(f"未知波形：{pattern}（用 coyote_waves 看全部可用波形）")
        result = await device.pulse(ch, pattern, intensity, int(float(duration_s or 3) * 1000))
        return {"success": True, **result}

    @Tool(
        "coyote_waves",
        description=(
            "列出可用波形（官方经典 + 程序化 + 人类自定义）。"
            "人类往 waveforms/user.json 里加了波形后，用 reload=true 让插件重新读盘，不用重启。"
        ),
        parameters=[
            ToolParameterInfo(name="keyword", param_type=ToolParamType.STRING, description="按名字/描述筛选，如「潮」", required=False),
            ToolParameterInfo(name="reload", param_type=ToolParamType.BOOLEAN, description="先重新读盘再列出（改了 user.json 后用）", required=False),
        ],
    )
    async def coyote_waves(self, keyword: str = "", reload: bool = False, **kwargs: Any) -> dict:
        del kwargs
        info = reload_waveforms() if reload else None
        items = catalog(keyword or "")
        return {
            "success": True,
            "count": len(items),
            "reloaded": info,
            "items": items,
        }

    @Tool(
        "coyote_stop",
        description="急停：清除波形并把强度归零。任何异常、不适、或人类说停，立刻调用。",
        parameters=[
            ToolParameterInfo(
                name="channel",
                param_type=ToolParamType.STRING,
                description='要停的通道："A"、"B" 或 "BOTH"（默认）',
                required=False,
            )
        ],
    )
    async def coyote_stop(self, channel: str = "BOTH", **kwargs: Any) -> dict:
        del kwargs
        device = self._require()
        ch = normalize_channel(channel, "BOTH")
        if ch == "BOTH":
            result = await device.emergency_stop("tool call")
            return {"success": True, "channels": ["A", "B"], **result}
        await device.clear(ch)
        return {"success": True, "channels": [ch], "strength": device.strength}

    # ---------------- 命令 ----------------
    @Command(
        "coyote",
        description="郊狼控制：/coyote pair|status|on|off|stop|limit <n>|waves",
        pattern=r"^[/!！.]coyote(?:\s+(.*))?$",
        aliases=["/郊狼"],
    )
    async def handle_coyote(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        raw = str(kwargs.get("text") or kwargs.get("plain_text") or "")
        match = re.search(r"coyote\s*(.*)$", raw, re.IGNORECASE) or re.search(r"郊狼\s*(.*)$", raw)
        args = (match.group(1) if match else "").strip().split()
        action = (args[0].lower() if args else "status")

        try:
            device = self._require()
        except RuntimeError as exc:
            await self._reply(stream_id, f"郊狼插件没跑起来：{exc}")
            return False, str(exc), True

        if action in ("pair", "配对", "扫码", "qrcode", "qr"):
            return True, await self._send_pairing(stream_id), True

        if action in ("status", "状态"):
            text = self._summary()
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("on", "arm", "解锁"):
            await device.set_armed(True)
            text = "已解锁，允许驱动。急停发 /coyote stop"
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("off", "lock", "锁定"):
            await device.set_armed(False)
            text = "已锁定并清零"
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("stop", "急停", "停"):
            result = await device.emergency_stop("command")
            text = f"已急停，强度归零（A={result['strength']['A']} B={result['strength']['B']}）"
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("boost", "临时", "加一下"):
            try:
                ch, delta, seconds = self._parse_boost(args[1:])
                result = await device.boost(ch, delta, int(seconds * 1000))
            except ValueError as exc:
                text = f"参数不对：{exc}"
                await self._reply(stream_id, text)
                return True, text, True
            if not result.get("changed"):
                text = result.get("message") or "没有变化"
            else:
                text = (
                    f"临时强度：{result['channel']} 通道 {result['previous']} → {result['strength']}，"
                    f"{seconds:g} 秒后自动回到 {result['previous']}"
                )
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("waves", "wave", "波形", "波形列表"):
            # /coyote waves            → 列出全部
            # /coyote waves reload     → 重新读盘（改了 user.json 之后用）
            # /coyote waves 潮         → 按关键词筛
            rest = args[1:]
            if rest and rest[0].lower() in ("reload", "重载", "刷新"):
                info = reload_waveforms()
                lines = [f"波形库已重载：官方 {info['official']} 套、自定义 {info['user']} 套、程序化 {info['builtin']} 种"]
                if info["errors"]:
                    lines.append("有定义被跳过：\n" + "\n".join(f"- {e}" for e in info["errors"]))
                lines.append(f"自定义文件：{info['path']}")
                text = "\n".join(lines)
                await self._reply(stream_id, text)
                return True, text, True
            keyword = " ".join(rest).strip()
            items = catalog(keyword)
            if not items:
                text = f"没有匹配「{keyword}」的波形"
                await self._reply(stream_id, text)
                return True, text, True
            by_source: dict[str, list[str]] = {}
            for item in items:
                by_source.setdefault(item["source"], []).append(item["name"])
            label = {"official": "官方经典", "user": "自定义", "builtin": "程序化"}
            parts = [f"{label.get(k, k)}（{len(v)}）：{'、'.join(v)}" for k, v in by_source.items()]
            text = f"共 {len(items)} 个波形\n" + "\n".join(parts)
            text += "\n用法：/coyote waves reload 重载 | 自己的波形写在 waveforms/user.json"
            await self._reply(stream_id, text)
            return True, text, True

        if action in ("limit", "上限") and len(args) > 1:
            value = _clamp_int(args[1], self._cfg["max_strength"], 1, 200)
            self._cfg["max_strength"] = value
            if self.device is not None:
                self.device.cfg["max_strength"] = value
            text = f"本次运行强度上限已改为 {value}（配置文件里的值不会变）"
            await self._reply(stream_id, text)
            return True, text, True

        text = (
            "用法：/coyote pair | status | on | off | stop | limit <1-200> | "
            "boost [A|B] <增量> <秒> | waves [reload|关键词]"
        )
        await self._reply(stream_id, text)
        return True, text, True

    def _parse_boost(self, args: list[str]) -> tuple[str, int, float]:
        """解析 boost 参数，允许省略通道：``A 10 5`` / ``10 5``。"""
        ch = self._cfg["default_channel"]
        rest = list(args)
        if rest and rest[0].upper() in ("A", "B"):
            ch = rest.pop(0).upper()
        if len(rest) < 2:
            raise ValueError("需要 <增量> <秒>，例如 /coyote boost A 10 5")
        try:
            delta = int(float(rest[0]))
        except (TypeError, ValueError):
            raise ValueError(f"增量不是数字：{rest[0]}") from None
        try:
            seconds = float(rest[1])
        except (TypeError, ValueError):
            raise ValueError(f"秒数不是数字：{rest[1]}") from None
        if seconds <= 0:
            raise ValueError("秒数必须大于 0")
        return ch, delta, seconds

    async def _send_pairing(self, stream_id: str) -> str:
        if self.relay is None:
            text = "中继未启动"
            await self._reply(stream_id, text)
            return text
        if self.relay.paired:
            text = "已经连上了，直接用就好。"
            await self._reply(stream_id, text)
            return text

        candidates = self.relay.candidate_urls()
        if not candidates:
            text = "探测不到可用地址，请在配置里手动填 public_host"
            await self._reply(stream_id, text)
            return text

        # 只发一个地址时不用解释，直接发；有两个以上就逐个标注，按场景扫。
        lines: list[str] = []
        sent_images = 0
        for item in candidates:
            lines.append(f"{item['label']}：{item['url']}")
            if len(candidates) == 1:
                caption = (
                    "手机与电脑连同一个 Wi-Fi，打开 DG-LAB 4.0 App → Socket 控制 → 扫一扫，"
                    "扫下面这张码："
                )
            else:
                caption = f"【{item['label']}】扫这张："
            image = qr_png_base64(self.relay.qr_payload_for(item["url"]))
            if image and stream_id:
                try:
                    if await self.ctx.send.image(image, stream_id):
                        await self._reply(stream_id, caption)
                        sent_images += 1
                        continue
                except Exception:  # noqa: BLE001
                    self.ctx.logger.exception("发送二维码图片失败")
            # 图片发不出去就退化成纯文本
            await self._reply(stream_id, f"{caption}\n{item['url']}")

        # 二维码里编码的是「取码那一刻」的 IP。地址一变这张码就废了，
        # 这一点必须写在提示里，否则人类会以为码坏了/插件坏了。
        tail = (
            "二维码里的地址是取码那一刻的 IP，路由器重启或 DHCP 续租后可能失效——"
            "连不上就再发一次 /coyote pair 拿最新的。"
        )
        if len(candidates) > 1:
            hint = (
                "本机有多块能用的网卡，所以给了几张码。手机和电脑连同一个 Wi-Fi 时，"
                "扫「同一 Wi-Fi」那张就行；如果扫了连不上，换另一张试试。"
                + tail
            )
        else:
            hint = "扫码后点「连接」，连上告诉我一声。" + tail
        if sent_images:
            await self._reply(stream_id, hint)
        return " / ".join(lines)

    # ---------------- 首页卡片 ----------------
    @HomeCard(
        # 注意：组件名在同一插件内必须唯一。Host 的 component_registry 遇到同名组件是「覆盖」，
        # 卡片若也叫 coyote_status 会把同名工具顶掉，所以这里用 coyote_home。
        "coyote_home",
        title="郊狼状态",
        description="DG-LAB 连接与强度概览",
        content="打开后点击刷新查看当前强度与解锁状态。",
        width="small",
        order=200,
    )
    async def home_card_marker(self) -> None:
        return None


def create_plugin() -> MaiBotPlugin:
    return CoyotePlugin()
