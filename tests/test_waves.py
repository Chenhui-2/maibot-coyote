# -*- coding: utf-8 -*-
"""波形库测试：官方 24 套 + 自定义 + 通道映射 + 分片。

要钉住的行为：
    1) official.json 能完整加载，且每一帧都是合法的 16 位十六进制
    2) 双通道帧要按通道取半边；目标半边是平线时借用另一半
       （否则「在 A 通道放呼吸」会得到一条直线，看起来像坏了）
    3) 用户自定义支持 levels（0-100 的数字）和 data（现成帧）两种写法
    4) 写错的条目只跳过并记警告，不能让整个波形库挂掉
    5) 超过 100 帧要分片（官方单次下发上限）
    6) 未知波形给明确报错，不静默播空

运行：python tests/test_waves.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

from coyote import waves


def ok(message: str) -> None:
    print(f"✓ {message}")


def main() -> None:
    info = waves.reload_waveforms()
    assert info["official"] == 24, f"官方波形应为 24 套，实际 {info['official']}"
    assert not info["errors"], f"官方波形加载报错：{info['errors']}"
    ok(f"官方经典波形 {info['official']} 套加载成功（{info['user']} 套自定义）")

    entries = waves.load()
    bad = []
    for name, entry in entries.items():
        for frame in entry.get("frames") or []:
            if len(frame) != 16 or any(c not in "0123456789ABCDEF" for c in frame):
                bad.append(f"{name}:{frame}")
    assert not bad, f"有非法帧：{bad[:3]}"
    total = sum(len(e.get("frames") or []) for e in entries.values())
    ok(f"{total} 帧全部合法（16 位十六进制）")

    # 官方波形都在清单里，且能按名字查到
    for name in ("呼吸", "潮汐", "快速按捏", "挑逗2", "信号灯"):
        assert name in entries, f"缺少官方波形 {name}"
    ok("抽查官方波形：呼吸/潮汐/快速按捏/挑逗2/信号灯 都在")

    # ---- 通道映射 ----
    # 呼吸：A 半边是平线（10），应借用 B 半边的起伏
    frames_a = waves.build_frames("呼吸", 100, "A", 1200)
    assert len(frames_a) == 12
    assert len(set(frames_a)) >= 4, f"呼吸在 A 通道退化成平线：{frames_a}"
    assert all(f[8:16] == "00000000" for f in frames_a), "A 通道不该往 B 半边走数据"
    ok(f"呼吸 → A 通道借用起伏（{len(set(frames_a))} 种不同帧）")

    # 压缩：B 半边恒定 100，应借用 A 半边的压缩斜坡
    frames_b = waves.build_frames("压缩", 100, "B", 1000)
    assert len(set(frames_b)) >= 4, f"压缩在 B 通道退化成平线：{frames_b}"
    assert all(f[0:8] == "00000000" for f in frames_b), "B 通道不该往 A 半边走数据"
    ok(f"压缩 → B 通道借用斜坡（{len(set(frames_b))} 种不同帧）")

    # 强度缩放：100 与 50 的峰值应差一半左右
    peak_full = max(int(f[0:2], 16) for f in waves.build_frames("潮汐", 100, "A", 2300))
    peak_half = max(int(f[0:2], 16) for f in waves.build_frames("潮汐", 50, "A", 2300))
    assert 0.4 <= peak_half / max(1, peak_full) <= 0.6, f"强度缩放不对：{peak_full} vs {peak_half}"
    ok(f"强度缩放生效：100% 峰值 {peak_full} → 50% 峰值 {peak_half}")

    # ---- 自定义写法 ----
    entry = waves._normalize({"name": "测试-数字", "levels": "0, 50 100 50"}, "user")
    assert entry and entry["levels"] == [0.0, 50.0, 100.0, 50.0], entry
    waves._registry[entry["name"]] = entry  # 模拟从 user.json 读到
    try:
        frames = waves.build_frames("测试-数字", 100, "A", 400)
        assert frames[2] == "6464646400000000", frames
        assert len(frames) == 4, frames
    finally:
        waves._registry.pop(entry["name"], None)
    ok("自定义 levels 写法：逗号/空格分隔都认，100 生成 64，短波形循环")

    entry = waves._normalize({"name": "测试-帧", "data": "0a0a0a0a00000000 0A0A0A0A64646464"}, "user")
    assert entry and entry["frames"] == ["0A0A0A0A00000000", "0A0A0A0A64646464"], entry
    ok("自定义 data 写法：小写自动转大写，按空格切分")

    # ---- 坏数据只跳过，不炸 ----
    assert waves._normalize({"name": "", "levels": "1 2"}, "user") is None
    assert waves._normalize({"name": "坏的", "data": "ZZZZ"}, "user") is None
    assert waves._normalize({"name": "空的"}, "user") is None
    assert waves._normalize("不是对象", "user") is None
    ok(f"坏定义被跳过并记警告（累计 {len(waves._errors)} 条），波形库不受影响")

    # ---- 分片 ----
    assert [len(c) for c in waves.chunk_frames(["0" * 16] * 250)] == [100, 100, 50]
    assert [len(c) for c in waves.chunk_frames(["0" * 16] * 100)] == [100]
    ok("超过 100 帧按官方上限分片：250 → 100/100/50")

    # ---- 未知波形 ----
    try:
        waves.build_frames("根本没有这个波形", 60, "A", 1000)
    except ValueError as exc:
        assert "未知波形" in str(exc), exc
        ok("未知波形给出明确报错")
    else:
        raise AssertionError("未知波形应该报错")

    # 解析别名 / 英文 key 仍然可用
    assert waves.resolve_pattern("breathe") == "breathe"
    assert waves.resolve_pattern("呼吸") == "呼吸"
    assert waves.resolve_pattern("波浪") == "波浪涟漪"  # 模糊匹配
    ok("解析：breathe → 程序化，呼吸 → 官方，波浪 → 波浪涟漪（模糊）")

    print(f"\n波形库测试全部通过 ✅（共 {info['total']} 个波形）")


if __name__ == "__main__":
    main()
