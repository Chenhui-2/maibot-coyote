# -*- coding: utf-8 -*-
"""内嵌 DG-LAB Socket V4 中继（协议对齐官方 dungeonlab-open/dglab-websocket-server 的 v4-server.ts）。

链路：本插件（控制方） ⇄ 本模块的中继 ⇄ DG-LAB 4.0 App（被控方，扫码接入）
插件自身不走 WebSocket，直接以进程内控制方身份挂在中继上，少一跳、少一处故障点。

协议（逐条对照官方 v4-server.ts）：
    控制方连接（无 tid）      → 回 {"type":"hello","clientId":...}
    被控方连接（?tid=控制方） → 被控方收 controller_attached，控制方收 client_attached
    控制方下发               → {"type":"message","clientId":被控方,"data":{...}}
    被控方上报               → {"type":"message","data":{...}}，转发时补 clientId
    业务探活                 → 收 {"type":"ping"} 必须回 {"type":"pong","ts":...}
    心跳                     → 服务端每 30s 广播 {"type":"heartbeat"}（无 clientId）
    断开                     → 被控方收 controller_disconnected + close(4000)

关于"自动断连"：官方对**业务层 ping** 有专门处理（handlePingMessage）。App 会定期发
{"type":"ping"} 探活，服务端不回 pong 时 App 会主动断开重连。同时官方 WS 层是
10s ping + 容忍 3 次未响应（约 30s）才 terminate，比 websockets 默认的
ping_timeout=20 宽松。这两条必须严格照做，否则 App 会周期性掉线。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import secrets
import socket
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

try:  # websockets >= 13
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - 兼容旧版本
    from websockets.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("coyote.relay")

# 业务心跳广播间隔（官方 HEARTBEAT_INTERVAL 默认 30000）
HEARTBEAT_S = 30.0
# WS 层 ping 间隔（官方 WS_PING_INTERVAL 默认 10000）
WS_PING_INTERVAL_S = 10.0
# 容忍多少秒没收到 pong 才判定连接死亡。
# 官方是 3 次未响应 ≈ 30s，但手机在 Wi-Fi 边缘漫游、息屏瞬间切频段时，
# 单次 pong 丢包很常见，30s 太紧，放到 60s 给足重传余量。
WS_PING_TIMEOUT_S = 60.0

# 官方关闭码
CLOSE_CONTROLLER_DISCONNECTED = 4000
CLOSE_CONTROLLER_NOT_FOUND = 4001
CLOSE_IDLE_TIMEOUT = 4002
# 本插件自定义：拒绝无 tid 的连接（官方没这个码，用 4003 不冲突）
CLOSE_SPECTATOR_DENIED = 4003


# 虚拟网卡/隧道网卡的名字特征。这些网卡上的地址手机是连不上的，
# 一旦被当成配对地址发出去，表现就是"扫了码连不上"，而且很难查。
_VIRTUAL_HINTS = (
    "vmnet", "vmware", "virtualbox", "vboxnet", "hyper-v", "vethernet",
    "docker", "br-", "veth", "virbr", "wsl", "loopback",
    "tap-", "tun", "utun", "awdl", "llw", "npcap", "bluetooth",
    "pdp_ip", "rmnet", "teredo", "isatap",
)

# 地址探测结果的缓存。TTL 要短——用户换 Wi-Fi / 拔网线 / DHCP 续租之后，
# 缓存太久会一直发旧地址出去，二维码看着正常但连不上。
_LAN_CACHE: dict[str, Any] = {"at": 0.0, "items": []}
LAN_CACHE_TTL_S = 3.0


def _route_ip() -> str:
    """默认路由出口 IP。UDP connect 只做路由表查询，不会真的发包。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            sock.connect(("223.5.5.5", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def _iface_score(name: str, ip: str) -> tuple[bool, int]:
    """返回 (是否虚拟网卡, 排序分)。分越低越可能是手机能连上的地址。"""
    low = (name or "").lower()
    virtual = any(hint in low for hint in _VIRTUAL_HINTS)
    if ip.startswith("192.168."):
        score = 0
    elif ip.startswith("10."):
        score = 1
    elif re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
        score = 2
    else:
        score = 5
    # 手机连的是 Wi-Fi，无线网卡命中同一网段的概率最高
    if any(hint in low for hint in ("wlan", "wi-fi", "wifi", "无线", "wlan0")):
        score -= 1
    if virtual:
        score += 100
    return virtual, score


def _is_private_ip(ip: str) -> bool:
    """RFC1918 私有网段。手机在同一局域网时用的必然是这三段之一。"""
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    return bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip))


def _local_ipv4() -> list[str]:
    """枚举本机所有 IPv4（不依赖 psutil）。"""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    route = _route_ip()
    if route and route not in ips:
        ips.append(route)
    return ips


def _routable(ip: str) -> bool:
    """这个源地址能不能出网。只查路由表，不真的发包（1-2ms）。

    VMware / Hyper-V / VirtualBox 的 host-only 网卡**没有默认路由**，
    这一步正好把它们筛掉——麻烦的是它们也常用 192.168.x.x，
    光看网段跟真网卡完全分不出来（一台装了虚拟机的机器上同时存在
    真实网卡和两三块 host-only 网卡是常态，猜错就永远连不上）。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.35)
            sock.bind((ip, 0))
            sock.connect(("223.5.5.5", 80))
            return True
    except OSError:
        return False


def _probe_lan() -> list[dict]:
    """枚举所有可能被手机访问到的 IPv4 地址，按可信度排序。

    关键点：**不假设只有一个地址**。DHCP 续租换网段、同时插网线和 Wi-Fi、
    开着虚拟网卡，都会让"只有一个正确地址"的假设失效。全部列出来让人挑，
    比自动猜一个然后猜错要好。

    筛选顺序：
        1. 只留 RFC1918 私有地址（公网、CGNAT 的 100.64/10 之类自然被排除）
        2. 排除没有出网路由的（虚拟 host-only 网卡）
        3. 全都不满足（比如完全离线的局域网）就退回按网段排
    """
    route_ip = _route_ip()
    # psutil 只用来取网卡**名字**（让标签更好认），不是必需品。
    names: dict[str, str] = {}
    try:
        import psutil  # type: ignore

        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    names[addr.address] = name
    except Exception:  # noqa: BLE001 - 没有 psutil 完全不影响主流程
        pass

    private = [ip for ip in _local_ipv4() if _is_private_ip(ip)]
    # 每个地址只查一次路由表（一次 1–2ms，别重复查）
    routable = {ip: _routable(ip) for ip in private}
    chosen = [ip for ip in private if routable[ip]] or private
    if not chosen and _is_private_ip(route_ip):
        chosen = [route_ip]

    items: list[dict] = []
    for ip in chosen:
        iface = names.get(ip, "")
        virtual, score = _iface_score(iface, ip)
        if virtual:
            continue
        if ip == route_ip:
            score -= 2  # 默认路由出口最可能是真的在用那块网卡
        elif not routable.get(ip, True):
            score += 10  # 没有出网路由：可能是离线局域网，也可能是不该用的网卡
        items.append({"ip": ip, "iface": iface, "score": score})

    items.sort(key=lambda item: (item["score"], item["ip"]))
    return items


def lan_candidates(force: bool = False) -> list[dict]:
    """带短 TTL 缓存的局域网地址列表。"""
    now = time.monotonic()
    if not force and _LAN_CACHE["items"] and now - _LAN_CACHE["at"] < LAN_CACHE_TTL_S:
        return list(_LAN_CACHE["items"])
    items = _probe_lan()
    _LAN_CACHE["at"] = now
    _LAN_CACHE["items"] = items
    return list(items)


def lan_address() -> str:
    """取本机局域网 IPv4（最可信的那个），供手机扫码访问。"""
    items = lan_candidates()
    return items[0]["ip"] if items else "127.0.0.1"


def _conn_path(ws: Any) -> str:
    """兼容 websockets 各版本读取请求路径。"""
    path = getattr(ws, "path", None)
    if path:
        return path
    request = getattr(ws, "request", None)
    path = getattr(request, "path", None)
    return path or ""


def _peer_of(ws: Any) -> str:
    """取对端地址，用来在状态里显示手机是从哪个网段连进来的。

    - ``192.168.x.x`` / ``10.x.x.x`` / ``172.16-31.x.x`` → 同一局域网
    - 其它地址 → 说明不是从局域网进来的（本插件不支持这种用法，应当拒绝）
    """
    addr = getattr(ws, "remote_address", None)
    if isinstance(addr, tuple) and addr:
        return str(addr[0])
    return ""


class CoyoteRelay:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9998,
        public_host: str = "",
        log: logging.Logger | None = None,
        allow_spectator: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.public_host = public_host or ""
        self.allow_spectator = bool(allow_spectator)
        self.client_id = secrets.token_hex(6)
        self.targets: dict[str, Any] = {}
        self.peers: dict[str, str] = {}
        self._server = None
        self._heartbeat_task: asyncio.Task | None = None
        self._listeners: dict[str, list[Callable[..., Any]]] = {
            "attached": [],
            "detached": [],
            "message": [],
        }
        self.error: str | None = None
        # 传入插件自己的 logger，否则 stdlib 的 coyote.relay 在 Runner 里会被
        # root logger 的 WARNING 级别挡掉，连接/断开事件一条都进不了主日志。
        self._log = log or logger
        # 断连诊断：最近一次断开的原因，便于 /coyote status 直接看到
        self.last_disconnect: dict[str, Any] | None = None
        self.disconnect_count = 0
        # 地址变化跟踪：局域网地址会因为 DHCP 续租、切 Wi-Fi、拔网线而变。
        # 变了必须主动说出来——旧二维码当场失效，不说的话人类只会觉得"连不上了"。
        self._address_seen: str = ""
        self._address_at_start: str = ""
        self.last_address_change: dict[str, Any] | None = None
        self.address_change_count = 0

    # ---------- 地址 ----------
    @property
    def address(self) -> str:
        return self.public_host or lan_address()

    @property
    def pair_ws_url(self) -> str:
        return f"ws://{self.address}:{self.port}?tid={self.client_id}"

    @property
    def pair_qr_payload(self) -> str:
        return self.qr_payload_for(self.pair_ws_url)

    @staticmethod
    def qr_payload_for(url: str) -> str:
        """把 ws 地址包成 DG-LAB App 能识别的扫码内容。"""
        from urllib.parse import quote

        return f"https://dungeon-lab.cn/s/?v=1&action=socket&url={quote(url, safe='')}"

    @property
    def paired(self) -> bool:
        return bool(self.targets)

    @property
    def address_changed(self) -> bool:
        """当前地址与启动时是否不同（不同 = 启动时发的二维码已失效）。"""
        return bool(self._address_at_start) and self._address_seen != self._address_at_start

    def candidate_urls(self) -> list[dict]:
        """列出所有可用的配对地址，按优先级排序。

        - 指定了 public_host 就只用它（用户明确知道自己在干什么）
        - 否则列出**所有**能用的局域网地址（多网卡 / 换过网段时全给出来）

        本插件面向同一局域网使用，所以这里只会给出本机物理网卡的局域网地址。
        多给几个地址的成本只是多一张二维码，比"只猜一个然后猜错、扫码连不上、
        还得回头查 IP"便宜得多。
        """
        out: list[dict] = []
        seen: set[str] = set()

        def add(label: str, host: str, kind: str, iface: str = "") -> None:
            if not host or host in seen:
                return
            seen.add(host)
            out.append({
                "label": label,
                "kind": kind,
                "host": host,
                "iface": iface,
                "url": f"ws://{host}:{self.port}?tid={self.client_id}",
            })

        if self.public_host:
            add("指定地址", self.public_host, "manual")
            return out

        lans = lan_candidates()
        for item in lans[:3]:
            if len(lans) == 1:
                label = "同一 Wi-Fi"
            else:
                # 有 psutil 就用网卡名，没有就用地址本身（都比"自动探测"有用）
                label = f"同一 Wi-Fi（{item['iface'] or item['ip']}）"
            add(label, item["ip"], "lan", item["iface"])
        return out

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        # 对齐官方：10s ping，容忍 30s 没 pong 才断（原来是 20/20，手机后台就掉）。
        self._server = await ws_serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=WS_PING_INTERVAL_S,
            ping_timeout=WS_PING_TIMEOUT_S,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        # 记下启动时的地址，之后每次心跳都对比一次，变了就告警。
        self._address_seen = lan_address()
        self._address_at_start = self._address_seen
        self._log.info("郊狼中继已启动 %s", self.pair_ws_url)

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        # 按官方做法：先告诉 App 是「控制方断开」，再关连接。
        # 这样 App 显示"控制方已断开"，而不是当成网络故障反复重连。
        for tid, ws in list(self.targets.items()):
            try:
                await ws.send(
                    json.dumps(
                        {"type": "controller_disconnected", "clientId": self.client_id},
                        ensure_ascii=False,
                    )
                )
                await ws.close(CLOSE_CONTROLLER_DISCONNECTED, "controller_disconnected")
            except Exception:  # noqa: BLE001
                pass
            self._log.info("中继停止，已通知被控方 %s", tid)
        self.targets.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ---------- 事件 ----------
    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self._listeners.setdefault(event, []).append(callback)

    async def _emit(self, event: str, *args: Any) -> None:
        for callback in list(self._listeners.get(event, [])):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                self._log.exception("中继事件回调异常: %s", event)

    # ---------- 下发 ----------
    def send(self, data: dict, target_id: str | None = None) -> int:
        """向被控方（手机 App）下发一条业务消息，返回成功发送数。

        同步接口（工具调用处直接调）。发送走独立 task，异常必须自己吞掉，
        否则会冒成 "Task exception was never retrieved"。
        """
        sent = 0
        loop = asyncio.get_running_loop()
        for tid, ws in list(self.targets.items()):
            if target_id and tid != target_id:
                continue
            payload = json.dumps({"type": "message", "data": data}, ensure_ascii=False)
            task = loop.create_task(self._safe_send(ws, payload, tid))
            task.add_done_callback(lambda t: t.exception())
            sent += 1
        return sent

    async def _safe_send(self, ws: Any, payload: str, tid: str = "") -> bool:
        try:
            await ws.send(payload)
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.warning("下发失败 %s: %s", tid or "-", exc)
            return False

    async def _heartbeat_loop(self) -> None:
        # 官方心跳帧就是 {"type":"heartbeat"}，不带 clientId。
        frame = json.dumps({"type": "heartbeat"})
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            for tid, ws in list(self.targets.items()):
                if not await self._safe_send(ws, frame, tid):
                    self._log.warning("心跳发送失败，被控方可能已掉线: %s", tid)
            # 借心跳周期顺手复查一次局域网地址（探测走 3 秒缓存，几乎没有开销）。
            try:
                await self._check_address()
            except Exception:  # noqa: BLE001
                self._log.debug("地址复查失败", exc_info=True)

    async def _check_address(self) -> None:
        """局域网地址变了就告警并广播事件。

        这是"IP 会被改掉"这个问题的核心处理：插件没法阻止 DHCP 换地址，
        但可以让人类在第一次连不上之前就知道地址已经换了、该重新扫码了。
        """
        current = lan_address()
        if not current or current == "127.0.0.1":
            return  # 暂时没有可用网络，不当成"地址变化"
        if not self._address_seen:
            self._address_seen = current
            return
        if current == self._address_seen:
            return

        previous = self._address_seen
        self._address_seen = current
        self.address_change_count += 1
        self.last_address_change = {
            "at": time.strftime("%H:%M:%S"),
            "from": previous,
            "to": current,
        }
        self._log.warning(
            "本机局域网地址已变化：%s → %s。旧二维码已失效，请重新发 /coyote pair",
            previous,
            current,
        )
        await self._emit("address_changed", previous, current)

    # ---------- 连接处理 ----------
    async def _handler(self, ws: Any) -> None:
        query = parse_qs(urlparse(_conn_path(ws)).query)
        # 官方同时接受 targetId 和 tid
        tid = (query.get("tid") or query.get("targetId") or [""])[0]

        if not tid:
            # 没有 tid 就不是合法的被控方——App 一定带着 tid 连进来。
            #
            # 官方 v4-server.ts 里，刚接入的控制方 controlledClients 是空的，
            # 它发不出任何东西。这里对齐官方：默认直接拒绝。否则任何人只要连上
            # 端口就能把 device.op 下发给已配对的设备，等于未授权控制。
            #
            # 确实需要只读调试时，才显式打开 allow_spectator。
            if not self.allow_spectator:
                await ws.send(
                    json.dumps(
                        {"type": "error", "code": "bad_request", "message": "缺少 tid 参数"},
                        ensure_ascii=False,
                    )
                )
                await ws.close(CLOSE_SPECTATOR_DENIED, "tid_required")
                self._log.warning(
                    "拒绝无 tid 的连接（来源 %s）——这可能是扫描器或未授权的控制方",
                    _peer_of(ws) or "-",
                )
                return

            # 显式开启调试旁观时，仍然只读：能看到帧，但一个字都发不出去。
            await ws.send(json.dumps({"type": "hello", "clientId": self.client_id}))
            self._log.warning(
                "旁观模式已开启（只读），来源 %s", _peer_of(ws) or "-"
            )
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if self._handle_ping(ws, frame):
                    continue
                self._log.info("旁观者试图下发（已忽略）：%s", str(frame)[:120])
            return

        if tid != self.client_id:
            await ws.send(
                json.dumps(
                    {"type": "error", "code": "controller_not_found", "message": "控制方不存在"},
                    ensure_ascii=False,
                )
            )
            await ws.close(CLOSE_CONTROLLER_NOT_FOUND, "controller_not_found")
            self._log.warning("被控方拒绝接入：控制方 %s 不存在", tid)
            return

        target_id = secrets.token_hex(6)
        self.targets[target_id] = ws
        peer = _peer_of(ws)
        self.peers[target_id] = peer
        await ws.send(json.dumps({"type": "hello", "clientId": target_id}))
        await ws.send(json.dumps({"type": "controller_attached", "clientId": self.client_id}))
        await self._emit("attached", target_id)
        self._log.info(
            "手机 App 已接入: %s（当前 %d 个连接，来源 %s）", target_id, len(self.targets), peer or "-"
        )

        close_code: int | None = None
        close_reason = ""
        error_text = ""
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    self._log.debug("收到非 JSON 帧，已忽略")
                    continue
                # 业务层探活：必须回 pong，否则 App 会判定连接已死并主动断开。
                if self._handle_ping(ws, frame):
                    continue
                if frame.get("type") == "heartbeat":
                    continue
                if frame.get("type") != "message" or not isinstance(frame.get("data"), dict):
                    continue
                await self._emit("message", frame["data"], target_id)
        except ConnectionClosed:
            # 正常断开（含我们自己 close(4000) 造成的回环），不算异常
            pass
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
        finally:
            close_code = getattr(ws, "close_code", None)
            close_reason = str(getattr(ws, "close_reason", "") or "")
            peer = self.peers.pop(target_id, "") or peer
            self.targets.pop(target_id, None)
            self.disconnect_count += 1
            self.last_disconnect = {
                "at": time.strftime("%H:%M:%S"),
                "target_id": target_id,
                "peer": peer,
                "close_code": close_code,
                "close_reason": close_reason,
                "error": error_text,
            }
            # 断连原因必须打出来，否则只能靠猜（之前 info 被 root logger 挡掉，
            # 导致"总是自动断连"完全无从排查）。
            self._log.warning(
                "手机 App 已断开: %s close_code=%s reason=%s peer=%s%s",
                target_id,
                close_code if close_code is not None else "-",
                close_reason or "-",
                peer or "-",
                f" error={error_text}" if error_text else "",
            )
            await self._emit("detached", target_id)

    @staticmethod
    def _handle_ping(ws: Any, frame: Any) -> bool:
        """处理业务层 ping/pong，返回是否已消费该帧。

        官方 v4-server.ts 的 handlePingMessage：收到 {"type":"ping"} 必须回
        {"type":"pong","ts":...}；收到 {"type":"pong"} 直接忽略。
        """
        if not isinstance(frame, dict):
            return False
        kind = frame.get("type")
        if kind == "pong":
            return True
        if kind != "ping":
            return False
        try:
            asyncio.get_running_loop().create_task(
                ws.send(json.dumps({"type": "pong", "ts": int(time.time() * 1000)}))
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "client_id": self.client_id,
            "paired": self.paired,
            "targets": list(self.targets),
            "peers": dict(self.peers),
            "ws_url": self.pair_ws_url,
            "address": self.address,
            # 所有候选地址：手机连不上时，先看这里有没有对的那一个
            "lan_candidates": lan_candidates(),
            # 当前地址与启动时是否不同（不同 = 启动时发的二维码已失效）
            "address_changed": self.address_changed,
            "last_address_change": self.last_address_change,
            "address_change_count": self.address_change_count,
            "error": self.error,
            "disconnect_count": self.disconnect_count,
            "last_disconnect": self.last_disconnect,
        }
