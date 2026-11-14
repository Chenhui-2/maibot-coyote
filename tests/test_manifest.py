# -*- coding: utf-8 -*-
"""Manifest 与能力声明的一致性校验。

做两件事：
    1) 用 MaiBot 自己的 ManifestValidator 校验 _manifest.json（字段、版本区间、id 格式）；
    2) **静态对账**：扫出 plugin.py 里所有 ``self.ctx.<能力>.<方法>()`` 调用，
       逐个检查是否已在 manifest 的 capabilities 里声明。

第 2 项是这次「/coyote status 没回信」事故的回归防线：
MaiBot 的 ``AuthorizationManager.check_capability`` 只放行 manifest 里声明过的能力，
漏声明不会在加载期报错，只会在**调用时**返回 E_CAPABILITY_DENIED —— 表现就是
命令执行成功但人类侧什么都收不到，日志里也只有一行 "命令执行成功"。

运行（必须在 MaiBot 根目录，用它的解释器）：
    cd <MaiBot>
    <实例>/python-env/python.exe plugins/maibot-coyote/tests/test_manifest.py

不在 MaiBot 根目录时会跳过 ManifestValidator，只跑静态对账（CI 用的就是这一档）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
MANIFEST = PLUGIN_DIR / "_manifest.json"
PLUGIN_PY = PLUGIN_DIR / "plugin.py"

# 这些能力由 Runner 在插件进程内直接注入，不走 RPC，因此不需要在 manifest 声明。
LOCAL_ONLY = {
    "logger",  # 本地 stdlib logging 桥
    "paths",  # 本地路径对象
}


def ok(message: str) -> None:
    print(f"✓ {message}")


def collect_ctx_calls(source: str) -> set[str]:
    """扫出 ``self.ctx.<能力代理>.<方法>()`` 形式，拼成 ``能力代理.方法``。

    SDK 里能力名就是 ``call_capability("send.text", ...)`` 的第一个参数，
    而代理类的方法名与能力名后段一致（``SendCapability.text`` → ``send.text``）。
    """
    used: set[str] = set()
    for cap, method in re.findall(r"self\.ctx\.([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\s*\(", source):
        if cap in LOCAL_ONLY:
            continue
        used.add(f"{cap}.{method}")
    return used


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = set(manifest.get("capabilities") or [])

    # ---- 0. 结构、id 格式、必填字段 ----
    plugin_id = manifest["id"]
    assert re.match(r"^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$", plugin_id), (
        f"id 不符合 MaiBot 规范：{plugin_id}（必须是 命名空间.插件名 形式）"
    )
    for field in ("manifest_version", "version", "name", "description", "license", "author", "urls"):
        assert manifest.get(field), f"manifest 缺少必填字段：{field}"
    ok(f"id 格式合规：{plugin_id}（v{manifest['version']}）")

    # ---- 1. 真校验器 ----
    validator = None
    try:
        sys.path.insert(0, str(Path.cwd()))
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator  # type: ignore

        result = ManifestValidator().load_from_plugin_path(PLUGIN_DIR)
        errors = list(getattr(result, "errors", []) or [])
        warnings = list(getattr(result, "warnings", []) or [])
        assert not errors, f"manifest 校验失败: {errors}"
        ok(f"ManifestValidator 通过（warnings={warnings or '无'}）")
        validator = result
    except ImportError:
        print("· 跳过 ManifestValidator（当前目录不是 MaiBot 根目录）")

    if validator is not None:
        plugin_id = getattr(getattr(validator, "manifest", None), "id", None) or manifest["id"]
        caps = getattr(getattr(validator, "manifest", None), "capabilities", None) or sorted(declared)
        ok(f"id={plugin_id} | capabilities={sorted(caps)}")

    # ---- 2. 能力声明对账 ----
    source = PLUGIN_PY.read_text(encoding="utf-8")
    used = collect_ctx_calls(source)
    ok(f"代码中实际调用的能力：{sorted(used) or '无'}")

    missing = sorted(used - declared)
    assert not missing, (
        f"这些能力被调用但没在 _manifest.json 的 capabilities 里声明：{missing}\n"
        f"运行时会被 AuthorizationManager 拒绝（E_CAPABILITY_DENIED），"
        f"表现是命令执行成功但收不到回信。"
    )
    ok("能力声明完整（无漏声明）")

    extra = sorted(declared - used)
    if extra:
        print(f"· 声明但未使用（无害）：{extra}")

    print("\nManifest 与能力一致性通过 ✅")


if __name__ == "__main__":
    main()
