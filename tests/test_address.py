# -*- coding: utf-8 -*-
"""地址探测与「IP 变了」的回归测试。

要钉住的行为：

    1) 网段分类：只有 RFC1918 才算局域网，公网 / CGNAT 段都不算
    2) 虚拟网卡过滤：VMware / Hyper-V 的 host-only 网卡也用 192.168.x，
       但它们没有出网路由，必须被筛掉（猜错就永远连不上）
    3) 多候选：同时有多个真网卡时全部列出，不擅自只留一个
    4) public_host 优先：用户明确指定了地址就只用它
    5) IP 变化能被发现：地址变了要留下记录并置 address_changed，
       否则用户只会觉得"二维码突然连不上了"

运行：python tests/test_address.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

from coyote import relay as relay_mod
from coyote.relay import (
    CoyoteRelay,
    _is_private_ip,
    lan_candidates,
)

PORT = 19996

# 全部用文档里约定的示例网段（RFC 5737 / RFC1918），不含任何真实机器信息。
REAL_NIC = "192.168.1.100"      # 接路由器的那块真网卡（默认路由出口）
SECOND_NIC = "10.0.0.9"         # 第二块真网卡（有线 + 无线同时插）
CGNAT_IP = "100.101.102.103"    # 100.64/10（CGNAT 段），不属于局域网
PUBLIC_IP = "203.0.113.9"       # RFC 5737 TEST-NET-3，公网示例段
VMNET_IPS = ("192.168.56.1", "192.168.138.1")  # VMware host-only，无默认路由


def ok(message: str) -> None:
    print(f"✓ {message}")


def test_classify() -> None:
    assert _is_private_ip(REAL_NIC)
    assert _is_private_ip("10.1.2.3")
    assert _is_private_ip("172.16.0.1") and _is_private_ip("172.31.255.254")
    assert not _is_private_ip("172.32.0.1")
    assert not _is_private_ip(PUBLIC_IP)
    assert not _is_private_ip(CGNAT_IP), "100.64/10 不是局域网地址"
    ok("网段分类：RFC1918 判定正确，公网与 CGNAT 段排除")


def test_probe_filters_virtual(monkeypatch_ips: list[str], monkeypatch_routable: set[str]) -> None:
    """伪造一块 VMware host-only 网卡，确认它不会变成配对地址。"""
    real_local, real_routable, real_route = (
        relay_mod._local_ipv4,
        relay_mod._routable,
        relay_mod._route_ip,
    )
    relay_mod._local_ipv4 = lambda: list(monkeypatch_ips)
    relay_mod._routable = lambda ip: ip in monkeypatch_routable
    relay_mod._route_ip = lambda: REAL_NIC
    try:
        items = relay_mod._probe_lan()
    finally:
        relay_mod._local_ipv4, relay_mod._routable, relay_mod._route_ip = (
            real_local,
            real_routable,
            real_route,
        )

    ips = [item["ip"] for item in items]
    assert ips and ips[0] == REAL_NIC, f"真网卡应排第一，实际 {ips}"
    for vmnet in VMNET_IPS:
        assert vmnet not in ips, f"VMware host-only 网卡未被筛掉：{ips}"
    assert PUBLIC_IP not in ips, f"公网段未被筛掉：{ips}"
    assert CGNAT_IP not in ips, f"CGNAT 段混进局域网候选：{ips}"
    ok(f"虚拟网卡被筛掉，只留 {ips}")


def test_multi_candidate() -> None:
    real_local, real_routable, real_route = (
        relay_mod._local_ipv4,
        relay_mod._routable,
        relay_mod._route_ip,
    )
    relay_mod._local_ipv4 = lambda: [REAL_NIC, SECOND_NIC]
    relay_mod._routable = lambda ip: True
    relay_mod._route_ip = lambda: REAL_NIC
    relay_mod._LAN_CACHE.update({"at": 0.0, "items": []})
    try:
        relay = CoyoteRelay(host="127.0.0.1", port=PORT)
        urls = relay.candidate_urls()
    finally:
        relay_mod._local_ipv4, relay_mod._routable, relay_mod._route_ip = (
            real_local,
            real_routable,
            real_route,
        )
        relay_mod._LAN_CACHE.update({"at": 0.0, "items": []})

    lan = [u for u in urls if u["kind"] == "lan"]
    assert len(lan) == 2, f"两块真网卡都该列出来，实际 {[u['url'] for u in lan]}"
    assert lan[0]["host"] == REAL_NIC, f"默认路由出口应排第一，实际 {lan[0]['host']}"
    assert all(u["url"].startswith("ws://") for u in urls)
    ok("多网卡全部列为候选，顺序按可信度")


def test_public_host_wins() -> None:
    relay = CoyoteRelay(host="127.0.0.1", port=PORT, public_host="192.168.9.9")
    urls = relay.candidate_urls()
    assert len(urls) == 1 and urls[0]["host"] == "192.168.9.9", urls
    assert relay.address == "192.168.9.9"
    ok("public_host 指定后不再自动探测")


async def test_address_change() -> None:
    relay = CoyoteRelay(host="127.0.0.1", port=PORT)
    await relay.start()
    try:
        # 假装启动时是旧地址，现在的探测结果与之不同 → 必须报变化
        relay._address_seen = "192.168.0.10"
        relay._address_at_start = "192.168.0.10"
        await relay._check_address()

        change = relay.last_address_change
        assert change is not None, "地址变化没有被记录"
        assert change["from"] == "192.168.0.10", change
        assert change["to"] != "192.168.0.10", change
        assert relay.address_change_count == 1, relay.address_change_count
        assert relay.status()["address_changed"] is True
        ok(f"地址变化被发现：{change['from']} → {change['to']}")

        # 同一个地址再查一次不该重复计数（否则每次心跳都刷一条告警）
        await relay._check_address()
        assert relay.address_change_count == 1, "地址没变却重复告警"
        ok("地址未再变化时不重复告警")
    finally:
        await relay.stop()


def main() -> None:
    test_classify()
    test_probe_filters_virtual(
        monkeypatch_ips=[REAL_NIC, PUBLIC_IP, CGNAT_IP, *VMNET_IPS],
        monkeypatch_routable={REAL_NIC, PUBLIC_IP},
    )
    test_multi_candidate()
    test_public_host_wins()
    asyncio.run(test_address_change())

    # 真实环境自检：只要确认拿得到至少一个地址，不打印具体 IP（避免 CI 日志泄露内网拓扑）
    items = lan_candidates(force=True)
    assert items, "真实环境探测不到任何局域网地址"
    assert all(_is_private_ip(item["ip"]) for item in items), items
    ok(f"真实环境探测到 {len(items)} 个可用地址")

    print("\n地址探测与 IP 变化处理全部通过 ✅")


if __name__ == "__main__":
    main()
