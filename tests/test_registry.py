# -*- coding: utf-8 -*-
"""把插件声明的组件真正喂给 Host 的 ComponentRegistry。

比"能 import"更强的一层：注册表会校验命令正则能否编译、工具参数 schema 是否合法、
同名组件是否互相覆盖。任何一项不合规都会抛 ComponentRegistrationError。

运行（必须在 MaiBot 根目录，且用它的解释器）：
    cd <MaiBot>
    <实例>/python-env/python.exe plugins/maibot-coyote/tests/test_registry.py

不在 MaiBot 根目录时本脚本会自行跳过（CI 里只跑与 Host 无关的测试）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
# id / 模块名都从 manifest 推导，避免改名后测试与真实插件对不上。
_MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = _MANIFEST["id"]
MODULE_NAME = PLUGIN_ID.replace(".", "_").replace("-", "_")


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, str(PLUGIN_DIR / "plugin.py"), submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module.create_plugin()


def main() -> None:
    sys.path.insert(0, str(Path.cwd()))
    try:
        from src.plugin_runtime.host.component_registry import ComponentRegistry  # type: ignore
    except ImportError:
        print("· 跳过：当前目录不是 MaiBot 根目录（本测试需要 Host 的 ComponentRegistry）")
        return

    instance = load_plugin()
    components = instance.get_components()
    registry = ComponentRegistry()

    registered = []
    for component in components:
        ok = registry.register_component(
            name=component["name"],
            component_type=component["type"],
            plugin_id=PLUGIN_ID,
            metadata=dict(component.get("metadata") or {}),
        )
        assert ok
        registered.append(f"{component['type']}:{component['name']}")

    print(f"✓ {len(registered)} 个组件全部注册成功")
    for item in sorted(registered):
        print("   ", item)

    # 同名覆盖检查：注册完后再数一遍，数量必须一致
    total = len(registry.get_all_components()) if hasattr(registry, "get_all_components") else None
    if total is not None:
        assert total == len(components), f"组件被覆盖：期望 {len(components)}，实际 {total}"
        print(f"✓ 无同名覆盖（注册表内 {total} 个）")

    # 命令正则真的能匹配
    match = registry.find_command_by_text("/coyote status")
    assert match is not None, "命令 /coyote status 匹配不到"
    entry, groups = match
    print(f"✓ 命令匹配：/coyote status → {entry.name}（捕获组 {groups}）")
    assert registry.find_command_by_text("/郊狼 状态") is not None, "中文别名匹配不到"
    print("✓ 命令别名：/郊狼 状态")

    print("\n注册表校验通过 ✅")


if __name__ == "__main__":
    main()
