# -*- coding: utf-8 -*-
"""把配对二维码渲染成图片（base64 PNG），方便直接发到聊天里。"""
from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger("coyote.qr")


def qr_png_base64(payload: str) -> str | None:
    """返回 base64 PNG；缺少 qrcode/Pillow 时返回 None，由调用方退回纯文本地址。"""
    try:
        import qrcode  # noqa: PLC0415
        from qrcode.constants import ERROR_CORRECT_M  # noqa: PLC0415
    except ImportError:
        logger.warning("未安装 qrcode，配对二维码将退回文本地址（pip install \"qrcode[pil]\"）")
        return None

    try:
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        logger.exception("生成二维码失败")
        return None


def qr_ascii(payload: str) -> str | None:
    """终端/纯文本场景下的字符二维码（部分客户端等宽字体可扫）。"""
    try:
        import qrcode  # noqa: PLC0415
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        lines = []
        for row in matrix:
            lines.append("".join("██" if cell else "  " for cell in row))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return None
