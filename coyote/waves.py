# -*- coding: utf-8 -*-
"""波形库：内置程序化波形 + 官方经典波形 + 用户自定义。

帧格式（DG-LAB V3/V4）：8 字节 = 16 个十六进制字符，前 4 字节 A 通道、后 4 字节 B 通道，
每字节 0x00–0x64（0–100 的相对幅度）。一帧 = 100ms，幅度调制通道当前强度。

三层来源，后者覆盖前者（同名即覆盖，方便"改官方波形"）：

    1. 内置程序化   SHAPES 里的 9 种，用公式生成，不占空间
    2. 官方经典     waveforms/official.json，App 内置的 24 套（真实帧数据）
    3. 用户自定义   waveforms/user.json，改完 /coyote waves reload 即时生效

用户自定义支持两种写法，都不需要懂帧格式：

    {"name": "我的波浪", "levels": "0 20 40 60 80 100 80 60 40 20"}   # 0–100 的相对幅度
    {"name": "抄来的",  "data": "0A0A0A0A00000000 0A0A0A0A64646464"}   # 现成的 16 位帧

`levels` 会按选定通道生成帧，所以同一个波形在 A、B 通道都能用；
`data` 是双通道原始帧，播放时按通道取对应半边（该半边太平就借用另一半）。
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger("coyote.waves")

FRAME_MS = 100
# 官方单次下发最多 100 帧（10 秒），超过要分片
MAX_FRAMES_PER_SEND = 100

DATA_DIR = Path(__file__).resolve().parent.parent / "waveforms"
OFFICIAL_FILE = DATA_DIR / "official.json"
USER_FILE = DATA_DIR / "user.json"


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def hex_frame(level: float, channel: str) -> str:
    """生成一帧 16 位十六进制字符串；非目标通道填 00，避免串到另一路。"""
    byte = f"{int(round(_clamp(level, 0, 100))):02X}"
    off = "00"
    a = off if channel == "B" else byte
    b = off if channel == "A" else byte
    return f"{a}{a}{a}{a}{b}{b}{b}{b}"


def _noise(i: int, seed: int = 1) -> float:
    x = math.sin(i * 12.9898 + seed * 78.233) * 43758.5453
    return x - math.floor(x)


# ---------------- 内置程序化波形 ----------------
SHAPES = {
    # 恒定输出：相当于"持续按住"
    "steady": lambda i: 1.0,
    # 呼吸：4 秒一个完整起伏
    "breathe": lambda i: 0.08 + 0.92 * (0.5 - 0.5 * math.cos(2 * math.pi * i / 40)),
    # 心跳：双搏动 + 长停顿（24 帧 ≈ 2.4s 一个循环）
    "heartbeat": lambda i: _heartbeat(i),
    # 连击：短促三连
    "tap": lambda i: _tap(i),
    # 潮汐：6 秒一周期的涨落
    "tide": lambda i: _tide(i),
    # 渐强：阶梯式逐级增强
    "climb": lambda i: _clamp(0.15 + (i % 40 // 4) * 0.1, 0, 1),
    # 颗粒：高频细碎抖动
    "grain": lambda i: 0.35 + 0.65 * _noise(i, 7),
    # 挤压：先强脉冲后持续按压
    "press": lambda i: _press(i),
    # 雨点：不规则密集点击
    "rain": lambda i: 0.55 + 0.45 * _noise(i, 11) if _noise(i, 3) > 0.45 else 0.0,
}


def _heartbeat(i: int) -> float:
    p = i % 24
    if p < 3:
        return 1.0
    if p < 5:
        return 0.45
    if p < 8:
        return 0.9
    if p < 10:
        return 0.3
    return 0.0


def _tap(i: int) -> float:
    p = i % 12
    if p < 2:
        return 1.0
    if p < 4:
        return 0.0
    if p < 6:
        return 1.0
    if p < 8:
        return 0.0
    if p < 9:
        return 0.85
    return 0.0


def _tide(i: int) -> float:
    p = (i % 60) / 60
    return 0.1 + 0.9 * (p * 2 if p < 0.5 else (1 - p) * 2)


def _press(i: int) -> float:
    p = i % 30
    if p < 4:
        return 1.0
    if p < 6:
        return 0.55
    return 0.7


BUILTIN = {
    "steady": ("恒定", "持续输出，强度不波动"),
    "breathe": ("呼吸(程序化)", "4 秒一次的缓慢起伏"),
    "heartbeat": ("心跳(程序化)", "双搏动 + 停顿的节律"),
    "tap": ("连击(程序化)", "短促的三连击"),
    "tide": ("潮汐(程序化)", "6 秒一周期的涨落"),
    "climb": ("渐强", "阶梯式逐级增强"),
    "grain": ("颗粒", "高频细碎抖动"),
    "press": ("挤压(程序化)", "先强脉冲后持续按压"),
    "rain": ("雨点", "不规则密集点击"),
}

ALIASES = {
    "恒定": "steady",
    "持续": "steady",
    "渐强": "climb",
    "颗粒": "grain",
    "雨点": "rain",
    "脉冲": "tap",
}


# ---------------- 外部波形（官方 / 用户） ----------------
_registry: dict[str, dict] | None = None
_errors: list[str] = []


def _read_json(path: Path) -> list[Any]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _errors.append(f"{path.name} 解析失败：{exc}")
        logger.warning("波形文件解析失败 %s：%s", path.name, exc)
        return []
    if isinstance(raw, dict):
        items = raw.get("waveforms") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return items if isinstance(items, list) else []


def _normalize(raw: Any, source: str) -> dict | None:
    """把一条波形定义规整成内部结构；不合法就跳过并记一条警告，绝不抛异常。"""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("key") or "").strip()
    if not name:
        return None
    desc = str(raw.get("desc") or raw.get("description") or "").strip()
    entry: dict[str, Any] = {"name": name, "desc": desc, "source": source}

    if raw.get("levels"):
        raw_levels = raw["levels"]
        if isinstance(raw_levels, str):
            parts = raw_levels.replace(",", " ").split()
        elif isinstance(raw_levels, (list, tuple)):
            parts = list(raw_levels)
        else:
            parts = []
        try:
            levels = [float(x) for x in parts]
        except (TypeError, ValueError):
            _errors.append(f"{name}：levels 里有非数字")
            return None
        if not levels:
            _errors.append(f"{name}：levels 为空")
            return None
        entry["levels"] = [_clamp(v, 0, 100) for v in levels]
        return entry

    raw_frames = raw.get("data") or raw.get("frames")
    if isinstance(raw_frames, str):
        frames = raw_frames.replace(",", " ").split()
    elif isinstance(raw_frames, (list, tuple)):
        frames = [str(x) for x in raw_frames]
    else:
        frames = []
    frames = [f.strip().upper() for f in frames if f and f.strip()]
    if not frames:
        _errors.append(f"{name}：既没有 levels 也没有 data/frames")
        return None
    bad = [f for f in frames if len(f) != 16 or any(c not in "0123456789ABCDEF" for c in f)]
    if bad:
        _errors.append(f"{name}：有 {len(bad)} 个非法帧（要 16 个十六进制字符），已跳过")
        return None
    entry["frames"] = frames
    return entry


def load(force: bool = False) -> dict[str, dict]:
    """读取 official.json + user.json，返回合并后的注册表。"""
    global _registry
    if _registry is not None and not force:
        return _registry

    _errors.clear()
    merged: dict[str, dict] = {}
    for path, source in ((OFFICIAL_FILE, "official"), (USER_FILE, "user")):
        for raw in _read_json(path):
            entry = _normalize(raw, source)
            if entry:
                merged[entry["name"]] = entry
    _registry = merged
    if _errors:
        logger.warning("波形库有 %d 条定义被跳过：%s", len(_errors), "；".join(_errors[:5]))
    return merged


def reload_waveforms() -> dict:
    """重新读盘，返回本次结果（供 /coyote waves reload 用）。"""
    entries = load(force=True)
    official = sum(1 for e in entries.values() if e["source"] == "official")
    user = sum(1 for e in entries.values() if e["source"] == "user")
    return {
        "official": official,
        "user": user,
        "builtin": len(BUILTIN),
        "total": len(entries) + len(BUILTIN),
        "errors": list(_errors),
        "path": str(USER_FILE),
    }


# ---------------- 解析与生成 ----------------
def _lookup(name: str) -> dict | None:
    """按名字找外部波形（官方 / 用户），支持名字包含匹配。"""
    entries = load()
    if name in entries:
        return entries[name]
    low = name.lower()
    for key, entry in entries.items():
        if key.lower() == low:
            return entry
    # 模糊：说「波浪」能命中「波浪涟漪」，说「渐强」能命中「按捏渐强」
    for key, entry in entries.items():
        if name in key or key in name:
            return entry
    return None


def resolve_pattern(name: str | None) -> str | None:
    """把用户输入解析成可用的波形标识（外部波形的名字，或内置 shape 的 key）。"""
    if not name:
        return "breathe"
    key = str(name).strip()
    if not key:
        return "breathe"
    if _lookup(key):
        return _lookup(key)["name"]  # type: ignore[index]
    if key in SHAPES:
        return key
    if key in ALIASES and ALIASES[key] in SHAPES:
        return ALIASES[key]
    low = key.lower()
    if low in SHAPES:
        return low
    return None


def _channel_levels(entry: dict, channel: str) -> list[float]:
    """取某个通道的幅度序列。

    官方帧是双通道的，两路内容不一样（比如「压缩」A 路在压缩、B 路顶住不放）。
    所以：优先用目标通道那半边；那半边几乎没有起伏（range < 5）时借用另一半，
    否则「在 A 通道放呼吸」会得到一条平线，看起来像坏了。
    """
    if "levels" in entry:
        return list(entry["levels"])
    frames = entry["frames"]
    a = [float(int(f[0:2], 16)) for f in frames]
    b = [float(int(f[8:10], 16)) for f in frames]
    pick, other = (a, b) if channel == "A" else (b, a)
    if max(pick) - min(pick) < 5 and max(other) - min(other) >= 5:
        return other
    return pick


def build_frames(pattern: str, intensity: float, channel: str, duration_ms: int) -> list[str]:
    key = resolve_pattern(pattern)
    if not key:
        raise ValueError(f"未知波形：{pattern}（用 /coyote waves 看全部可用波形）")

    total = max(1, round(duration_ms / FRAME_MS))
    amp = _clamp(float(intensity if intensity is not None else 60), 0, 100)

    if key in SHAPES:
        shape = SHAPES[key]
        return [hex_frame(shape(i) * amp, channel) for i in range(total)]

    entry = _lookup(key)
    if not entry:
        raise ValueError(f"波形 {pattern} 已失效，请 /coyote waves reload")
    levels = _channel_levels(entry, channel)
    if not levels:
        raise ValueError(f"波形 {pattern} 没有可用的幅度数据")
    scale = amp / 100.0
    return [hex_frame(levels[i % len(levels)] * scale, channel) for i in range(total)]


def chunk_frames(frames: list[str]) -> list[list[str]]:
    """按官方上限分片（单次最多 100 帧 = 10 秒）。"""
    size = MAX_FRAMES_PER_SEND
    return [frames[i : i + size] for i in range(0, len(frames), size)] or [[]]


def catalog(keyword: str = "") -> list[dict]:
    """给工具/命令用的波形清单。"""
    out: list[dict] = []
    for key, (cn, desc) in BUILTIN.items():
        out.append({"name": key, "cn": cn, "desc": desc, "source": "builtin", "frames": 0})
    for entry in load().values():
        out.append({
            "name": entry["name"],
            "cn": entry["name"],
            "desc": entry["desc"],
            "source": entry["source"],
            "frames": len(entry.get("frames") or entry.get("levels") or []),
        })
    if keyword:
        low = keyword.strip().lower()
        out = [
            item
            for item in out
            if low in item["name"].lower() or low in item["cn"].lower() or low in item["desc"].lower()
        ]
    return out


def pattern_list() -> str:
    """人类可读的波形清单，按来源分组。"""
    lines: list[str] = []
    entries = load()
    official = [e for e in entries.values() if e["source"] == "official"]
    user = [e for e in entries.values() if e["source"] == "user"]
    if official:
        lines.append(f"官方经典（{len(official)}）：" + "、".join(e["name"] for e in official))
    if user:
        lines.append(f"自定义（{len(user)}）：" + "、".join(e["name"] for e in user))
    lines.append(f"程序化（{len(BUILTIN)}）：" + "、".join(f"{k}={v[0]}" for k, v in BUILTIN.items()))
    return "\n".join(lines)
