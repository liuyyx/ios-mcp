# ios-mcp

通过 USB 控制 iPhone 的 [MCP](https://modelcontextprotocol.io/) server，基于
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 实现。支持
**iOS 17+**，可以给 Claude Desktop、Cursor 或任何 MCP 客户端使用。

## 功能

| MCP 工具 | 参数 | 返回 |
|---|---|---|
| `ios_list_devices` | — | JSON 数组：`udid` / `name` / `ios_version` / `connected` |
| `ios_screenshot` | `udid: str = ""` | base64 PNG（物理像素，无 `data:` 前缀） |
| `ios_tap` | `x: int, y: int, udid: str = ""` | 成功/失败 |
| `ios_swipe` | `x1, y1, x2, y2: int, duration_ms: int = 300, udid: str = ""` | 成功/失败 |
| `ios_scroll` | `direction: str, amount: int = 1, udid: str = ""` | 成功/失败 |
| `ios_input_text` | `text: str, udid: str = ""` | 成功/失败 |
| `ios_press_key` | `key: str, udid: str = ""` | 成功/失败（home / volume_up / volume_down / lock / siri） |
| `ios_screen_size` | `udid: str = ""` | `{"width": 393, "height": 852}`（逻辑点） |

所有 `udid` 参数都可以留空，此时使用第一台 USB 设备；也可以用环境变量
`IOS_MCP_UDID` 指定默认设备。

---

## 安装

要求 **Python 3.11+**，并且已安装 `usbmuxd` 所需的环境（见下一节）。

```bash
cd /home/liuyyx/projects/ios-mcp
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

如果想使用 `ios-mcp` 命令，再执行：

```bash
pip install -e .
```

> `requirements.txt` 中的版本是锁定的。HID（`com.apple.coredevice.hid.*`）
> 依赖 **pymobiledevice3 11.57.0** 引入的 CoreDevice HID 实现，不能降级到 4.x。

验证：

```bash
python -c "import ios_mcp"
python -m ios_mcp.server --help
```

---

## 前提条件（很重要）

1. **USB 数据线**：必须是能传数据的线，不能是“仅充电”线。
2. **解锁 iPhone**：锁屏状态下截图、触摸、启动 tunnel 都可能失败。
3. **信任此电脑**：首次连接时在 iPhone 上点“信任”，并输入锁屏密码。
4. **开启开发者模式**：
   `设置 > 隐私与安全性 > 开发者模式` → 打开 → 重启 iPhone → 再次确认。
5. **开发者磁盘镜像（DDI）**：
   - macOS 安装 **Apple Devices**（或 Xcode）后通常会自动准备；
   - 也可以手动挂载：
     ```bash
     pymobiledevice3 mounter auto-mount
     ```
   - 如果截图/HID 报 `InvalidServiceError`，基本都是 DDI 没挂载或开发者模式没开。
6. **usbmuxd**：
   - macOS：安装 Apple Devices（推荐）或 Xcode；
   - Linux：安装 `usbmuxd` / `libimobiledevice`；
   - Windows：安装 Apple Devices / iTunes，并使用其 usbmuxd 服务。
7. 同一时间只保留一个 iOS 17+ tunnel 更稳妥。Xcode Device Hub、QuickTime
   屏幕镜像等程序可能占用屏幕媒体流，导致 HID 鉴权失败；使用触摸前先关闭它们。

---

## 启动

### stdio（Claude Desktop / Cursor）

```bash
python -m ios_mcp.server
```

或安装后：

```bash
ios-mcp
```

### HTTP / SSE

```bash
python -m ios_mcp.server --transport sse --host 127.0.0.1 --port 8000
# 或
python -m ios_mcp.server --transport streamable-http --host 127.0.0.1 --port 8000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--transport` | `stdio` | `stdio` / `sse` / `streamable-http` |
| `--host` | `127.0.0.1` | SSE/HTTP 监听地址 |
| `--port` | `8000` | SSE/HTTP 端口 |
| `--log-level` | `INFO` | 日志级别，日志只写 stderr |
| `--version` | — | 显示版本 |

### 环境变量

| 变量 | 说明 |
|---|---|
| `IOS_MCP_UDID` | 默认设备 UDID（工具参数 `udid` 留空时使用） |
| `IOS_MCP_TUNNEL` | `userspace`（默认，无需 root）或 `tunneld` |
| `IOS_MCP_TUNNELD` | tunneld 地址，默认 `127.0.0.1:49151` |
| `IOS_MCP_RSD` | 直接连接已有 RSD，格式 `host:port` |
| `IOS_MCP_HID_IDLE_SECONDS` | HID 媒体流空闲关闭时间，默认 `120` 秒；`0` 表示不自动关闭 |
| `IOS_MCP_TRANSPORT` / `IOS_MCP_HOST` / `IOS_MCP_PORT` / `IOS_MCP_LOG_LEVEL` | 对应命令行参数的默认值 |

---

## 接入 Claude Desktop

编辑配置文件：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`
- Linux：`~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ios-control": {
      "command": "/home/liuyyx/projects/ios-mcp/.venv/bin/python",
      "args": ["-m", "ios_mcp.server"],
      "env": {
        "PYTHONPATH": "/home/liuyyx/projects/ios-mcp",
        "IOS_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

> 请把 `command` 换成你自己的 Python 绝对路径。GUI 应用通常拿不到 shell 的
> `PATH`，所以不建议只写 `python`。
>
> 如果已经 `pip install -e .`，也可以把 `command` 设为 `.venv/bin/ios-mcp`，
> 并省略 `args`。

修改后完全退出并重新打开 Claude Desktop。

## 接入 Cursor

编辑 `~/.cursor/mcp.json`（全局）或项目内的 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "ios-control": {
      "command": "/home/liuyyx/projects/ios-mcp/.venv/bin/python",
      "args": ["-m", "ios_mcp.server"],
      "env": {
        "PYTHONPATH": "/home/liuyyx/projects/ios-mcp"
      }
    }
  }
}
```

然后在 Cursor 的 MCP 设置中确认 `ios-control` 已连接（工具列表里应出现 8 个
`ios_*` 工具）。

---

## 坐标系统

- `ios_screenshot` 返回的是**物理像素**，例如 iPhone 15 Pro 为 `1179×2556`。
- `ios_tap` / `ios_swipe` / `ios_scroll` 接收**逻辑点**，例如 `393×852`。
  不确定时先调用 `ios_screen_size`。
- HID 内部使用 `0..65535` 的归一化坐标：`norm_x = x / width`。
- 坐标超出屏幕范围会返回明确错误，不会静默点击到边缘。
- 当前按竖屏逻辑尺寸换算；设备旋转后如需精确坐标，请重新确认界面尺寸。

`ios_scroll` 的方向表示“内容滚动方向”：

- `up`：手指从下往上滑（内容向上滚动，看后面的内容）；
- `down`：手指从上往下滑；
- `left` / `right`：同理。

---

## 实现说明

### 设备发现与 tunnel（`ios_mcp/device.py`）

- 通过 `pymobiledevice3.usbmux.list_devices()` 枚举 USB 设备；
- 通过 `create_using_usbmux()` 读取 `DeviceName`、`ProductVersion`；
- iOS 17+ 的开发者服务需要 RSD tunnel：
  - 默认使用 **用户态 tunnel**（`UserspaceRsdTunnel`），**不需要 root**；
  - 也可以先运行 `sudo pymobiledevice3 tunneld`，再设置
    `IOS_MCP_TUNNEL=tunneld` / `IOS_MCP_TUNNELD=127.0.0.1:49151`；
  - iOS 17.0–17.3 没有 CoreDeviceProxy，用户态 tunnel 不可用时请使用 tunneld。
- tunnel 会按 UDID 缓存，切换设备时自动关闭旧的再建立新的。

### 截图（`ios_mcp/screen.py`）

按顺序尝试：

1. CoreDevice `com.apple.coredevice.screencaptureservice`；
2. DVT `com.apple.instruments.server.services.screenshot`；
3. lockdown `com.apple.mobile.screenshotr`（需要开发者镜像）。

返回前用 Pillow 校验/转换为 PNG，保证客户端拿到的是合法 PNG。

### HID（`ios_mcp/hid.py`）

使用 iOS 17+ 的 CoreDevice HID 服务：

- `com.apple.coredevice.hid.universalhidservice`：触摸屏、虚拟键盘；
- `com.apple.coredevice.hid.indigo`：Home / 音量 / 锁屏 / Siri。

触摸和键盘需要一条活动媒体流，才能让 `backboardd` 把我们的 HID surface
标记为已鉴权。本实现会保持媒体流（画面数据丢弃），并在空闲 120 秒后自动关闭。

> **关于 iOS 版本**：pymobiledevice3 11.57 会以“iOS 27 以下不发送视频帧”
> 为由拒绝启动媒体流，但 `dtuhidd` 在 iOS 17–26 上仍会接受请求并解除 HID
> 鉴权。因此对 iOS < 27，本实现会临时跳过该版本检查，仅用于鉴权，不依赖
> 画面帧；iOS 27+ 直接走原生路径。该兼容路径未在本仓库的 CI 中用真机验证，
> 如果设备拒绝启动媒体流，会返回明确错误而不是假装成功。

文本输入：

- ASCII：虚拟键盘逐字输入（支持 Shift、回车、Tab 等）；
- 含中文等非 ASCII 字符：写入 iPhone 剪贴板并模拟 `Cmd+V`。
  这会覆盖设备原有的剪贴板内容。

硬件按键映射：

| key | HID usage |
|---|---|
| `home` | Consumer 0x40 (Menu) |
| `volume_up` | Consumer 0xE9 |
| `volume_down` | Consumer 0xEA |
| `lock` | Consumer 0x30 (Power) |
| `siri` | Consumer 0xCF (Voice Command) |

---

## 常见错误

| 现象 | 处理 |
|---|---|
| `连接不到 usbmuxd` | 安装 Apple Devices / usbmuxd；重新插拔数据线；运行 `pymobiledevice3 doctor` |
| `iPhone 尚未信任这台电脑` | 解锁 iPhone，点“信任”，输入锁屏密码 |
| `没有检测到 iPhone` | 换一根支持数据传输的线；解锁屏幕；确认设备出现在 `pymobiledevice3 usbmux list` |
| `拒绝启动所需的开发者服务` / `InvalidServiceError` | 开启开发者模式并重启；运行 `pymobiledevice3 mounter auto-mount`；关闭 Xcode Device Hub / QuickTime 镜像 |
| 截图黑屏或失败 | 解锁并亮屏；等锁屏界面完全退出后重试 |
| HID 鉴权失败 | 关闭其它屏幕镜像程序；重新插拔设备；重启 MCP server；必要时重启 iPhone |
| 中文输入无效 | 先点击输入框获得焦点；Cmd+V 在部分 App 中不支持 |
| 多台设备时操作错设备 | 显式传 `udid`，或设置 `IOS_MCP_UDID` |

运行自检：

```bash
pymobiledevice3 doctor
pymobiledevice3 usbmux list
```

---

## 已知限制

- 只支持 USB 连接的 iPhone；网络配对设备未纳入默认设备发现。
- 一个进程同一时间只维护一个用户态 tunnel；多设备需要切换或使用 tunneld。
- 锁屏状态下无法截图/点击，这是 iOS 安全限制。
- 屏幕旋转后的坐标换算未做动态适配。
- 非 ASCII 文本依赖剪贴板和 `Cmd+V`，会覆盖剪贴板。
- HID 的媒体流鉴权路径需要在真机上验证；失败时会抛出带原因的错误。

## 安全提示

这个 server 相当于把你的 iPhone 输入能力暴露给 MCP 客户端。请只在本机运行
stdio 或绑定 `127.0.0.1` 的 HTTP 传输，不要把端口暴露到公网。
