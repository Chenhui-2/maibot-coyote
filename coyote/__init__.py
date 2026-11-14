# -*- coding: utf-8 -*-
"""郊狼（DG-LAB）控制插件的内部实现包。"""

from .device import DeviceController, normalize_channel
from .relay import CoyoteRelay, lan_address
from .waves import (
    BUILTIN,
    build_frames,
    catalog,
    chunk_frames,
    pattern_list,
    reload_waveforms,
    resolve_pattern,
)

__all__ = [
    "DeviceController",
    "normalize_channel",
    "CoyoteRelay",
    "lan_address",
    "BUILTIN",
    "build_frames",
    "catalog",
    "chunk_frames",
    "pattern_list",
    "reload_waveforms",
    "resolve_pattern",
]
