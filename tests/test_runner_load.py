# -*- coding: utf-8 -*-
"""按 MaiBot Runner 的真实加载方式验证入口契约。

复刻 src/plugin_runtime/runner/plugin_loader.py 的做法：
    spec_from_file_location(安全模块名, plugin.py, submodule_search_locations=[插件目录])
    → sys.modules[模块名] = module → exec_module → create_plugin()

这样能验证：
    1) 插件被当成「包」加载时，`from .coyote...` 的相对导入可用
    2) create_plugin() 返回的实例通过 Host 的 _validate_sdk_plugin_contract
    3) 组件能收集齐（7 工具 + 1 命令 + 1 卡片），且没有同名覆盖
    4) 模型侧**没有**解锁工具（解锁只能由人类发命令）

运行（用 MaiBot 自己的解释器最贴近真实环境）：
    <实例>/python-env/python.exe plugins/maibot-coyote/tests/test_runner_load.py

不在 MaiBot 根目录时，Host 契约校验部分会自行跳过。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
# 与 Host 的 _build_safe_module_name() 一致：把 id 里的 . 和 - 换成 _
_MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = _MANIFEST["id"]
MODULE_NAME = PLUGIN_ID.replace(".", "_").replace("-", "_")

EXPECTED_TOOLS = {
    "coyote_boost",
    "coyote_pair",
    "coyote_pulse",
    "coyote_status",
    "coyote_stop",
    "coyote_strength",
    "coyote_waves",
}
EXPECTED_OTHERS = {"coyote", "coyote_home"}  # 命令 / 首页卡片
# 解锁/锁定是人类的专属权限：模型侧**不允许**出现解锁工具
FORBIDDEN_TOOLS = {"coyote_arm", "coyote_unlock", "coyote_lock", "coyote_armed"}


def load_like_runner():
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader, "无法创建模块 spec"
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    module = load_like_runner()
    print("✓ 按 Runner 方式加载成功（包形态，相对导入可用）")

    assert hasattr(module, "create_plugin"), "缺少 create_plugin 工厂函数"
    instance = module.create_plugin()
    print("✓ create_plugin() 返回", type(instance).__name__)

    # Host 侧的 SDK 契约校验（on_load / on_unload / on_config_update 必须实现）
    try:
        sys.path.insert(0, str(Path.cwd()))  # 在 MaiBot 根目录下运行时才能导入 src
        from src.plugin_runtime.runner.plugin_loader import PluginLoader  # type: ignore

        PluginLoader._validate_sdk_plugin_contract(PLUGIN_ID, instance)
        print("✓ 通过 Host 的 _validate_sdk_plugin_contract")
    except ImportError:
        print("· 跳过 Host 契约校验（当前解释器没有 MaiBot 源码）")

    components = instance.get_components()
    names = sorted(c.get("name", "") for c in components)
    expected = EXPECTED_TOOLS | EXPECTED_OTHERS
    missing = expected - set(names)
    assert not missing, f"缺少组件: {missing}"
    assert len(names) == len(set(names)), f"存在同名组件（Host 会覆盖）: {names}"

    tool_names = {c.get("name") for c in components if str(c.get("type", "")).upper() == "TOOL"}
    assert tool_names == EXPECTED_TOOLS, (
        f"工具集合不符：多 {tool_names - EXPECTED_TOOLS} 少 {EXPECTED_TOOLS - tool_names}"
    )

    leaked = tool_names & FORBIDDEN_TOOLS
    assert not leaked, f"模型侧出现了解锁工具 {leaked}：解锁只能由人类发命令完成"

    print(f"✓ 组件收集正常（{len(components)} 个）: {', '.join(names)}")

    print("\n入口契约通过 ✅")


if __name__ == "__main__":
    main()
