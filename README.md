# maibot-coyote

> 给 [麦麦 MaiBot](https://github.com/Mai-with-u/MaiBot) 的 **DG-LAB 郊狼控制插件**，基于 `maibot-plugin-sdk` 编写。

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![sdk](https://img.shields.io/badge/maibot--sdk-2.x-green)
![manifest](https://img.shields.io/badge/manifest-v2-green)

插件自带 Socket V4 中继：手机 DG-LAB App 扫码接进来，麦麦就能通过一组工具读状态、调强度、打波形、急停。

> [!WARNING]
> 仅供**成年用户**在**自愿、知情、同意**前提下个人娱乐。
> 心脏病、心脏起搏器佩戴者勿用。电极不可跨心脏、不可置于颈部以上。
> 使用者对自己和对方的安全负全部责任。
>
> **请在确保无初始强度的情况下启动本插件**：先打开 DG-LAB App 把 A/B 两个通道都归零，
> 再启动 MaiBot。本插件以「设备当前强度为 0」为前提做增量控制。

---

## 特性

- **零配置接入**：内置中继，扫码即用，不需要另跑 DG-LAB 官方中继服务
- **7 个工具 + 1 条命令**：状态、配对、强度、临时强度、波形、急停全覆盖
- **解锁权归人类**：模型侧没有任何解锁工具，只有人类发命令才能解锁
- **安全夹死在代码里**：强度硬上限、单步限幅、显式解锁、断连清零、急停回锁
- **35 个波形**：官方经典 24 套（真实帧数据）+ 程序化 9 种 + 你自定义的任意套
- **IP 变了能自愈**：自动排除虚拟网卡、多网卡全列、地址变化主动告警

---

## 链路

```
MaiBot（本插件，进程内控制方）
   │  进程内调用，不走网络
   ▼
内嵌 Socket V4 中继  ws://0.0.0.0:9998
   │  ws://…?tid=<clientId>
   ▼
手机 DG-LAB 4.0 App（Socket 控制）
   │  BLE
   ▼
郊狼主机（A / B 双通道）
```

---

## 安装

> MaiBot 1.4+ 的插件系统要求目录里**必须有 `_manifest.json`**（Manifest v2，字段严格校验，
> 多一个未声明字段都会被拒）。本插件已按官方 schema 提供。

**1. 放进 `plugins/`**

```bash
cd <MaiBot>/plugins
git clone https://github.com/Chenhui-2/maibot-coyote.git
```

或者直接把整个目录复制过去。位置：

- 源码运行：`<MaiBot>/plugins/maibot-coyote/`
- 一键桌面版：`%APPDATA%\MaiBotOneKeyDesktop\<实例ID>\modules\MaiBot\plugins\maibot-coyote\`

**2. 装依赖**

一键桌面版自带的 `python-env` 里**通常已经装好** `websockets` 和 `qrcode`，先确认：

```bash
<实例>/python-env/python.exe -c "import websockets, qrcode; print('ok')"
```

缺了就用**同一个解释器**装（不要用系统 Python，否则插件进程看不到）：

```bash
<实例>/python-env/python.exe -m pip install -r plugins/maibot-coyote/requirements.txt
```

**3. 重启 MaiBot**，首次加载会在插件目录生成 `config.toml`，之后可以在 WebUI 的插件设置里改。

**4. 看日志确认三件事**：Manifest 通过、插件加载成功、中继起来。

```
郊狼中继已启动 ws=ws://192.168.x.x:9998?tid=...
```

---

## 项目结构

```
maibot-coyote/
├── _manifest.json      Manifest v2（缺了 Host 会拒绝加载）
├── plugin.py           入口，导出 create_plugin()
├── coyote/             内部实现
│   ├── relay.py        Socket V4 中继（握手 / 心跳 / 转发 / 地址探测）
│   ├── device.py       设备 RPC 与安全约束（夹取 / 解锁 / 临时强度）
│   ├── waves.py        波形库（加载 / 解析 / 分片）
│   └── qr.py           配对二维码渲染
├── waveforms/
│   ├── official.json   官方经典 24 套
│   └── user.json       ← 你加波形就改这个
├── tests/              测试脚本（见「测试」）
├── requirements.txt
└── LICENSE
```

---

## 用法

### 命令（人类侧）

| 命令 | 作用 |
|---|---|
| `/coyote pair`（或 `/郊狼 配对`） | 发一张配对二维码到当前聊天，扫码接入 |
| `/coyote status` | 连接状态 + A/B 强度 + 是否解锁（临时强度生效中会显示剩余秒数） |
| `/coyote on` | **解锁**，允许麦麦驱动设备（**只有人类能发**，模型没有对应工具） |
| `/coyote off` | 锁定并清零 |
| `/coyote stop` | 急停：清波形 + 归零 + 回锁（同时取消临时强度） |
| `/coyote limit 30` | 本次运行把强度上限改成 30（不改配置文件） |
| `/coyote boost A 10 5` | **临时强度**：A 通道 +10，5 秒后自动回到原值。通道可省略 |
| `/coyote waves` | 列出全部波形（`waves reload` 重载自定义波形，`waves 潮` 按关键词筛） |

### 工具（麦麦自己用）

| 工具 | 作用 |
|---|---|
| `coyote_status` | 状态 / 强度 / 上限 / 生效中的临时强度 |
| `coyote_pair` | 配对信息（ws 地址、二维码内容） |
| `coyote_strength` | 设绝对值或相对增减；带 `duration_s` 就是临时强度（到点回落原值） |
| `coyote_boost` | **临时提高强度一段时间**，到点自动回落，不用手动降回去 |
| `coyote_pulse` | 播放波形：官方经典 24 套 + 程序化 9 种 + 自定义，中英文名都认 |
| `coyote_waves` | 列出全部波形（可按关键词筛）；`reload=True` 重新读盘，改完 `user.json` 不用重启 |
| `coyote_stop` | 急停 |

> **解锁 / 锁定不在工具列表里。** 这是刻意的：解锁是唯一能"放行"模型的闸门。
> 如果模型自己就能解锁，前面所有强度上限、单步限幅、波形幅度约束都形同虚设。
> 所以插件**不向模型暴露任何解锁工具**，模型侧的每个驱动接口都先过 `_require_armed()`，
> 锁着就抛错。模型想动手，只能回头请人类发 `/coyote on`。
>
> 人类侧的命令才是唯一入口：`/coyote on` 解锁、`/coyote off` 锁定、`/coyote stop` 急停并回锁。
> `tests/test_runner_load.py` 会断言工具集合里没有 `coyote_arm` 之类的名字。

### 临时强度（boost）

「来一下，几秒就恢复」的场景用它，比自己调高再调低自然：

```
/coyote boost A 10 5     # A 通道 +10，5 秒后回到原来的值
/coyote boost 10 5       # 省略通道，用默认通道
```

实现用的是协议原生的 `t=4 SetTempIntensity`（App 侧到点自动回落），**外加一个本地兜底定时器**：
万一 App 不认 `d` 参数，本地也会把强度拉回去。只改本地状态、不发指令是不够的——那会变成
插件以为降了、设备还停在高位。`coyote_strength` 的 `duration_s` 走的是同一条路径。

几个边界：

- 增量受 `step_limit` 约束（默认 15），时长受 `max_boost_ms` 约束（默认 60s）
- 同通道重复 boost 会覆盖上一次
- 急停 / 锁定 / 断连会**立即取消**，回滚任务不会在急停之后把强度加回去
- 期间人类手动改过强度，到点就不回滚了（尊重手动操作）

### 波形库

三层，后加载的覆盖前面的（同名就覆盖，方便「改造」官方波形）：

| 来源 | 数量 | 位置 |
|---|---|---|
| 官方经典 | 24 套 | `waveforms/official.json` |
| 程序化 | 9 种 | `coyote/waves.py` 里的 `SHAPES`（公式生成，不占空间） |
| 用户自定义 | 任意 | `waveforms/user.json` |

官方 24 套（App 内置「经典波形」）：挤压、气泡、律动、电波、舞步、攀登、树荫、脉冲、潮汐、连击、
快速按捏、按捏渐强、心跳节奏、压缩、节奏步伐、颗粒摩擦、渐变弹跳、波浪涟漪、雨水冲刷、变速敲击、
信号灯、挑逗1、挑逗2、呼吸。

#### 加自己的波形

编辑 `waveforms/user.json`，然后发 `/coyote waves reload`（不用重启插件）。两种写法：

```json
{
  "waveforms": [
    { "name": "我的波浪", "desc": "匀升匀降", "levels": "0 10 20 30 40 50 60 70 80 90 100 90 80 70 60 50 40 30 20 10" },
    { "name": "抄来的", "desc": "现成帧", "data": "0A0A0A0A00000000 0A0A0A0A64646464" }
  ]
}
```

- **`levels`**：直接写 0–100 的相对幅度，**一个数 = 100ms**。最好写，也最不容易错。
- **`data`**：现成的 16 位十六进制帧（8 字节，前 4 字节 A 通道、后 4 字节 B 通道），从别处抄波形用这个。
- 名字重名会覆盖官方那一条；写错的条目只跳过并在重载时提示，不会让波形库挂掉。

几个约定：

- 波形只调制**幅度**，实际体感还受该通道当前强度约束——同一段波形，强度 10 和 30 完全是两回事。
- 播放时长比波形长时自动**循环**，短时截断；单次下发按官方上限 **100 帧（10 秒）分片**。
- 官方帧是双通道的，两路内容不同（如「压缩」A 路在压缩、B 路顶住不放）。播放时取目标通道那半边；
  那半边几乎是平线时**借用另一半**——否则「在 A 通道放呼吸」会得到一条直线，看着像坏了。

### 配对流程

1. **确认设备没有初始强度**：打开 DG-LAB App，把 A/B 两个通道都归零，再启动插件。
2. 手机和电脑连**同一个 Wi-Fi**。
3. 群里发 `/coyote pair`，机器人回一张二维码。
4. 手机 **DG-LAB 4.0 App → Socket 控制 → 扫一扫**。
5. App 内用蓝牙连上郊狼主机，`/coyote status` 里就能看到设备了。
6. `/coyote on` 解锁，然后就可以让麦麦动手了。

---

## 配置（`config.toml`）

```toml
[plugin]
config_version = "1.0.0"
enabled = true

[network]
host = "0.0.0.0"
port = 9998
public_host = ""         # 手机访问用的 IP，留空自动探测
allow_spectator = false  # 允许无 tid 的只读连接（调试用，公网暴露时务必保持 false）

[safety]
max_strength = 40        # 强度硬上限（1–200）
step_limit = 15          # 单次变化上限（1–40）
require_arm = true       # 需要人类先解锁
default_channel = "A"
max_pulse_ms = 10000     # 单次波形最长时长
max_wave_intensity = 80  # 波形相对幅度上限
max_boost_ms = 60000     # 临时强度单次最长时长
idle_zero_ms = 0         # 空闲自动归零，0=关闭
overheat_backoff = true  # 过热时上限压到 20
```

改完保存，MaiBot 会热重载配置；只有 `network.host` / `network.port` 变了才会重启中继。

---

## 安全设计

不是"提示模型注意安全"，是**代码里夹死**：

1. **强度硬上限**：目标值先夹到 `max_strength`，再换算成增量下发。
2. **单步限幅**：一次调用最多改变 `step_limit`，`0 → 40` 得走三次。
3. **显式解锁**：`require_arm` 为真时，没解锁的强度/波形指令直接抛错。解锁**只能由人类发命令**
   完成——插件不向模型暴露任何解锁工具，模型无法给自己放行。
4. **急停即回锁**：`coyote_stop` / `off` / 插件卸载都会清波形 + 归零 + 回锁，并**取消所有临时强度定时器**。
5. **临时强度有兜底**：`t=4` 之外还有本地回滚，且回滚前会校验强度没被手动改过。
6. **断连清零**：App 一断开，本地强度立即归零，重连要重新解锁。
7. **波形不越强度**：波形只调制幅度（0–100 相对值），实际体感仍受通道强度约束。
8. **过热降级**：App 上报过热时，上限临时压到 20。
9. **中继默认拒绝无 tid 连接**：`close(4003, "tid_required")`，避免同网段任何人都能控制设备；
   `allow_spectator` 打开后也只读，指令一律丢弃。

---

## 关于局域网 IP

家用路由器给的地址是租约制，**换 Wi-Fi、路由器重启、DHCP 续租、插拔网线都可能换 IP**。
插件对此做了四件事：

1. **不缓存旧地址**：地址探测只缓存 3 秒，`/coyote status` / `/coyote pair` 拿到的永远是当前 IP。
2. **自动排除连不上的网卡**：VMware / Hyper-V / VirtualBox 的 host-only 网卡也常用 `192.168.x.x`，
   但它们没有出网路由，手机根本连不上。插件用「bind + connect 查路由表」把它们筛掉（1–2ms/地址，
   不依赖 psutil）。
3. **多网卡全列出来**：同时插网线和 Wi-Fi 时，`/coyote pair` 会把每块网卡各发一张码，标注网卡名。
4. **IP 变了会告诉你**：心跳周期（30s）顺手复查一次，变了就写日志并置位，
   `/coyote status` 会显示 `⚠️ 本机 IP 已从 x 变为 y，旧二维码已失效，请重新 /coyote pair`。

> **二维码只对生成时的 IP 有效。** 连不上时的第一反应应该是「再发一次 `/coyote pair`」，
> 而不是怀疑 App 或设备。另外插件每次重载 `tid` 也会变，旧码同样失效。
>
> 想彻底免掉这件事，可以在路由器上给电脑做 **DHCP 静态地址绑定**（按 MAC 固定 IP），
> 或者在 `config.toml` 里把 `public_host` 写死成你固定的那个 IP。

---

## 适用范围

本插件面向**手机与电脑在同一局域网**的个人使用场景。配对信息只会给出本机物理网卡的
局域网地址，文档和配置项也都按这个前提写。

超出这个范围的用法不在支持范围内：不提供配置项，也不负责调试。

Windows 上手机连不上，先放行端口：

```powershell
netsh advfirewall firewall add rule name="coyote-relay" dir=in action=allow protocol=TCP localport=9998
```

---

## 与 MaiBot 框架的契约


| 契约 | Host 侧实现 | 本插件 |
|---|---|---|
| Manifest v2 必需 | `runner/manifest_validator.py::PluginManifest`，`extra="forbid"` | `_manifest.json` 提供全部必填字段，`capabilities`/`i18n`/`urls` 齐全 |
| 入口文件 | `plugin.py` 必须存在（`require_entrypoint`） | ✓ |
| 工厂函数 | 模块级 `create_plugin()` | ✓ |
| 加载方式 | `spec_from_file_location(..., submodule_search_locations=[插件目录])` → **插件是包** | 用 `from .coyote...` 相对导入 |
| 生命周期 | `_validate_sdk_plugin_contract` 要求 `on_load`/`on_unload`/`on_config_update` 都被覆写 | ✓ |
| 命令入参 | `invoke_args = {text, stream_id, group_id, platform, user_id, matched_groups, ...}` | 从 `text` 解析，`**kwargs` 吸收其余 |
| **命令回信** | `bot.py::_handle_command_processing_result` **只把返回值写日志**，不发送 | 每个分支都 `await self.ctx.send.text(...)` 主动发；返回值仅供 Host 日志/钩子 |
| 命令返回值 | `(success, response_text, intercept)` 三元组；`intercept` 为真时不再进 LLM | 返回 `(True, 文本, True)` |
| 组件命名 | `component_registry` 同名组件是**覆盖**（`组件 xxx 已存在，覆盖`） | 9 个组件名互不重复（卡片叫 `coyote_home`） |
| 能力授权 | `authorization.check_capability` 只放行 Manifest 里声明过的能力，漏声明**加载期不报错**、调用时返回 `E_CAPABILITY_DENIED` | 声明并只使用 `send.text` + `send.image` |
| 依赖 | `dependency_pipeline` 读 `dependencies` 校验版本 | `websockets>=12.0`、`qrcode>=7.4` |

> **踩过的坑：`/coyote status` 没回信。**
> 两个原因叠加：① `@Command` 的返回值不会自动发送，必须自己调 `ctx.send.text`；
> ② `send.text` 属于受授权能力，manifest 漏声明时调用被静默拒绝。
> 表现就是日志里 `命令执行成功: coyote`，但聊天里什么都没有。
> `tests/test_manifest.py` 会静态扫描 `self.ctx.*` 调用并与 `capabilities` 对账，防止再犯。

---

## 测试

八个脚本，**都不接真机**（用假 App 模拟 DG-LAB 被控方）。

前五个用任意 Python 3.10+ 就能跑：

```bash
python tests/test_smoke.py          # 端到端 + SDK 组件收集
python tests/test_plugin_runtime.py # 模拟 Runner 注入，跑命令与工具（含"回信真的发出去了"断言）
python tests/test_relay_protocol.py # 中继协议对齐官方 v4-server.ts：ping/pong、心跳、握手、关闭码
python tests/test_address.py        # 地址探测：虚拟网卡过滤、多候选、IP 变化检测
python tests/test_waves.py          # 波形库：24 套帧合法性、通道映射、自定义写法、分片
```

后三个要 `import src.plugin_runtime...`，所以得**在 MaiBot 根目录、用它的解释器**跑：

```bash
cd <MaiBot>
<实例>/python-env/python.exe plugins/maibot-coyote/tests/test_runner_load.py  # 包形态加载 + Host 契约
<实例>/python-env/python.exe plugins/maibot-coyote/tests/test_registry.py     # 组件注册表（正则/schema/覆盖）
<实例>/python-env/python.exe plugins/maibot-coyote/tests/test_manifest.py     # Manifest 字段 + 能力对账
```

不在 MaiBot 根目录时，后三个会自行跳过需要 Host 的部分，不会误报失败。

---

## 排错

| 现象 | 处理 |
|---|---|
| 日志里 `中继启动失败` | 9998 被占用（旧 relay？），改 `network.port` |
| 命令发了没反应 | 先看日志有没有 `命令执行成功: coyote`：有 → 回信没发出去（`send.text` 未授权）；没有 → 命令没匹配上（检查前缀/权限/命令管理里是否被停用） |
| **App 反复自动断连** | 见下方「断连排查」 |
| `/coyote pair` 只回了文字没有图 | `qrcode[pil]` 没装上，或当前聊天不支持图片 |
| 扫码后一直「未连接」 | 手机和电脑不在同一局域网 / 防火墙拦端口 / 二维码里的 IP 手机访问不到（改 `public_host`） |
| 已连接但没有设备 | App 内还没用蓝牙连郊狼主机 |
| 强度调不动 | 没解锁（`/coyote on`）、已到上限、或 App 侧过热降级 |
| 波形没感觉 | 该通道强度太低；波形只调制幅度 |
| 改完代码不生效 | `/pm plugin reload coyote.maibot-coyote`（或重启麦麦） |

### 断连排查

中继已按官方 `v4-server.ts` 对齐了探活逻辑，断连原因都会带 `close_code` 打在日志里：

```
手机 App 已断开: a1b2c3 close_code=1000 reason=-
```

`/coyote status` 在未连接时也会直接显示最近一次断开的关闭码和原因。对照表：

| close_code | 含义 | 处理 |
|---|---|---|
| `1000` | App 侧正常关闭（用户退出/切走） | 正常，重新扫码即可 |
| `1001` | 服务端关闭（插件卸载/重载/重启） | 正常，重载后需重新扫码（`tid` 会变） |
| `1005` | 没有关闭帧，底层连接被掐断 | 网络/Wi-Fi 切换；手机息屏后系统挂起 socket |
| `1006` | 异常断开 | 同上，多为 Wi-Fi 漫游或路由器踢连接 |
| `4000` | 控制方断开（我们主动关的） | 正常，插件停止时通知 App |
| `4001` | `tid` 不存在（控制方 id 变了） | 插件重载后 `clientId` 会重新生成，需重新扫码 |
| `4002` | 控制方空闲超时（官方行为） | 5 分钟内没 App 接入，官方实现会关控制器；本插件是进程内控制方，不受影响 |
| `4003` | 本插件拒绝的连接（无 `tid`） | 别把中继裸暴露到公网；调试只读请用 `allow_spectator` |

如果频繁 `1005`/`1006`：

1. **手机息屏/切后台** —— Android 会挂起后台 socket。DG-LAB App 需要在前台或允许后台运行。
2. **路由器/防火墙掐空闲连接** —— 本插件每 30s 发业务心跳、每 10s 发 WS ping，正常不会被判定空闲。
3. **Wi-Fi 漫游或双频切换** —— 手机在 2.4G/5G 之间切换会换 IP，WebSocket 必然断。固定一个频段。
4. **电脑休眠** —— 电脑睡眠时服务端进程挂起，App 侧超时断开。

> 探活参数是断连的头号原因：`ping_timeout` 太紧，或者没回业务层 `pong`，App 就会判定连接已死。
> 本插件用 10s ping / 60s 容忍，并完整实现 `ping → pong` 应答，与官方一致。

---

## 协议来源与致谢

- **V4 中继协议**：对照 [`dungeonlab-open/dglab-websocket-server`](https://github.com/dungeonlab-open/dglab-websocket-server)
  的 `v4-server.ts` **逐条重新实现**，未复制其源码（该项目 GPL-3.0，本插件 MIT）。
  对齐要点：业务层 `ping → pong{ts}`、心跳帧 `{"type":"heartbeat"}`（无 clientId）、
  握手顺序 `hello → controller_attached`、关闭码 4000/4001/4002、10s WS ping + 3 次容忍。
- **V4 设备 RPC 与波形帧格式**：参考 [`indhg/AI-for-Coyote`](https://github.com/indhg/AI-for-Coyote) 公开的协议说明。
- **官方波形数据**：转写自公开的 App 波形库（与 [`hyperzlib/DG-Lab-Coyote-Game-Hub`](https://github.com/hyperzlib/DG-Lab-Coyote-Game-Hub)
  的 `pulse.json5` 同源）。仅供个人使用，版权归 DG-LAB 所有。

---

## 许可

[MIT](LICENSE)

---


