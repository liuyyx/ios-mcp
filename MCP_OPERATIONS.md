# ios-mcp 操作文档

**Owner:** ios-mcp contributors  
**版本：** 1.0  
**最近验证：** 2026-11-18

本文面向 MCP 客户端和 AI agent，说明如何通过 `ios-control` 查看并操作 iPhone。安装、开发者模式、DDI、usbmuxd 与客户端配置见 [README](README.md)。

## 1. 操作前检查

- [ ] iPhone 已通过支持数据传输的 USB 线连接。
- [ ] iPhone 已解锁、亮屏并信任当前电脑。
- [ ] 开发者模式已开启，开发者磁盘镜像可用。
- [ ] MCP 客户端已连接 `ios-control`，并能发现 15 个 `ios_*` 工具。
- [ ] 多设备环境已确定目标 UDID；单设备可让 `udid` 保持空字符串。

先调用：

```json
{"name": "ios_list_devices", "arguments": {}}
```

确认目标设备 `connected: true`。必要时记录其 `udid`，后续每个工具都传入该值，避免操作错设备。

## 2. 核心行为

### 2.1 坐标

- 截图和 `pixel_box` 使用**物理像素**。
- 点击、滑动、滚动及 `point` 使用**逻辑点**。
- `ios_screen_size` 返回逻辑尺寸；`ios_screenshot` 返回物理像素尺寸。
- OCR 和模板工具返回的 `point` 已完成换算，可直接点击，不要再次除以缩放倍率。

例如真机截图为 `1125×2436` 像素时，逻辑尺寸可能是 `375×812` 点。

### 2.2 每次操作后的截图

以下工具成功后都会自动等待 UI 稳定，再返回**操作后的截图**：

- `ios_tap`
- `ios_swipe`
- `ios_scroll`
- `ios_input_text`
- `ios_press_key`
- `ios_tap_text`
- `ios_tap_image`

返回内容固定为：

1. `TextContent`：操作成功消息；
2. `ImageContent`：JPEG q=85 的操作后截图。

`settle_ms` 默认 `600`，传 `0` 表示操作后立即截图。**每次点击都已带截图，不要重复调用 `ios_screenshot` 来确认同一次点击。**

如果操作成功但反馈截图失败，结果仍保留操作成功文本，并追加 `（截图失败：原因）`，不会把已执行的操作误报为失败。

### 2.3 截图不保存到磁盘

server 不创建截图文件。截图仅在内存中转换为 PNG/JPEG，再以 base64 放进 MCP `ImageContent`；`ios_screenshot_base64` 也只返回字符串。调用方若需要留档，必须自行保存返回数据。

## 3. 标准操作流程

### 3.1 看懂屏幕并点击文字或 App

1. 调用 `ios_describe` 获取同一张截图和 OCR 结果。
2. 从 `texts` 中找到目标，检查 `point` 和 `pixel_box`。
3. 调用 `ios_tap_text`。
4. 直接检查该调用返回的 `ImageContent`，确认页面变化。

打开主屏“设置”的 MCP 调用示例：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "ios_tap_text",
    "arguments": {
      "text": "设置",
      "offset_y": -48
    }
  }
}
```

成功结果的结构如下，图片 base64 在此处缩略：

```json
{
  "content": [
    {"type": "text", "text": "✅ 已点击文本「设置」：(318, 285)"},
    {"type": "image", "mimeType": "image/jpeg", "data": "<JPEG base64>"}
  ],
  "isError": false
}
```

`offset_y=-48` 表示点击文本中心上方 48 个逻辑点，适用于常见主屏图标。普通按钮和列表文字通常保持默认值 `0`。

### 3.2 点击无文字图标

1. 准备从相同设备、分辨率和显示缩放截取的小模板图。
2. 将模板 PNG/JPEG 编码为 base64；允许 `data:image/png;base64,` 前缀。
3. 调用 `ios_find_image` 检查匹配数量、分数和坐标。
4. 确认目标后调用 `ios_tap_image`；多个匹配时用 `index` 选择。
5. 检查点击调用自动返回的截图。

```json
{
  "name": "ios_tap_image",
  "arguments": {
    "template_base64": "<模板图 base64>",
    "threshold": 0.8,
    "index": 0,
    "settle_ms": 600
  }
}
```

模板匹配对旋转、缩放和主题变化敏感。不要直接拿其他机型的截图当模板。

### 3.3 已知逻辑坐标时直接操作

1. 调用 `ios_screen_size` 确认边界。
2. 调用 `ios_tap`、`ios_swipe` 或 `ios_scroll`。
3. 检查操作调用返回的截图。

```json
{"name": "ios_tap", "arguments": {"x": 180, "y": 620}}
```

```json
{
  "name": "ios_swipe",
  "arguments": {"x1": 187, "y1": 650, "x2": 187, "y2": 180, "duration_ms": 300}
}
```

坐标越界会返回错误，不会静默把点击限制到屏幕边缘。

## 4. 工具参考

### 4.1 设备与屏幕

#### `ios_list_devices()`

返回 USB 设备的 `udid`、`name`、`ios_version` 和 `connected`。

#### `ios_screen_size(udid="")`

返回 `{"width": ..., "height": ...}`，单位为逻辑点。

#### `ios_screenshot(udid="", image_format="png")`

返回尺寸文本和 `ImageContent`。`image_format` 可选：

- `png`：无损，默认；
- `jpeg`：体积较小。

#### `ios_screenshot_base64(udid="")`

返回纯 base64 PNG 字符串，不带 `data:` 前缀。仅在调用方需要原始截图数据时使用；普通看图优先 `ios_screenshot`。

#### `ios_describe(udid="", max_texts=80)`

只截一次图，同时返回该 PNG 和 OCR JSON。JSON 字段：

- `pixel_size` / `point_size`：两套坐标尺寸；
- `count`：识别到的文本总数；
- `texts`：文本、置信度、像素框和逻辑点；
- `truncated`：是否因 `max_texts` 截断。

`max_texts=0` 只省略文本数组内容，仍返回截图和总条数。

### 4.2 坐标操作

#### `ios_tap(x, y, udid="", settle_ms=600)`

在逻辑坐标点击一次，返回成功文本和点击后 JPEG。

#### `ios_swipe(x1, y1, x2, y2, duration_ms=300, udid="", settle_ms=600)`

按逻辑坐标滑动，返回成功文本和滑动后 JPEG。`duration_ms` 会被限制在 50–10000ms。

#### `ios_scroll(direction, amount=1, udid="", settle_ms=600)`

从屏幕中心滚动，返回成功文本和滚动后 JPEG：

- `up` / `down` / `left` / `right`；
- 也接受 `上` / `下` / `左` / `右`；
- `amount` 为 1–10 次。

#### `ios_input_text(text, udid="", settle_ms=600)`

向当前焦点输入文本并返回输入后 JPEG。ASCII 通过虚拟键盘输入；包含中文等非 ASCII 字符时，会覆盖 iPhone 剪贴板并模拟 `Cmd+V`。调用前必须先点击输入框。

#### `ios_press_key(key, udid="", settle_ms=600)`

按下并松开硬件键，返回操作后 JPEG。支持：

- `home`
- `volume_up`
- `volume_down`
- `lock`
- `siri`

### 4.3 OCR 视觉定位

#### `ios_ocr(udid="")`

只返回当前屏幕全部 OCR 结果，不返回图片。需要同时看图时使用 `ios_describe`。

#### `ios_find_text(text, udid="", exact=false)`

返回：

```json
{
  "query": "设置",
  "exact": false,
  "pixel_size": {"width": 1125, "height": 2436},
  "point_size": {"width": 375, "height": 812},
  "count": 1,
  "matches": [
    {
      "text": "设置",
      "confidence": 0.9991,
      "pixel_box": {"x": 916, "y": 976, "width": 79, "height": 45},
      "point": {"x": 318, "y": 333}
    }
  ]
}
```

默认执行大小写不敏感的子串匹配；`exact=true` 要求完整匹配。

#### `ios_tap_text(text, udid="", exact=false, index=0, offset_y=0, settle_ms=600)`

查找文本、点击其中心加上 `offset_y` 的位置，并返回点击后 JPEG。

- 无匹配：返回错误，并列出当前识别到的前几条文本；
- 多个匹配：用 `index` 指定，从 0 开始；
- `index` 越界：返回错误并给出匹配总数。

### 4.4 模板图像定位

#### `ios_find_image(template_base64, udid="", threshold=0.8)`

返回 `template_size`、两套屏幕尺寸、`count` 和 `matches`。每个匹配项包含：

- `score`：归一化匹配分数；
- `pixel_box`：物理像素区域；
- `point`：区域中心的逻辑点。

模板至少需要 `4×4` 像素，且不能大于当前截图。

#### `ios_tap_image(template_base64, udid="", threshold=0.8, index=0, settle_ms=600)`

点击指定模板匹配项的中心，并返回点击后 JPEG。无匹配或 `index` 越界时返回错误，不执行点击。

## 5. 推荐调用策略

1. **先看再点**：未知界面先 `ios_describe`，不要猜坐标。
2. **优先文字**：有文字时使用 `ios_find_text` / `ios_tap_text`。
3. **纯图形再用模板**：先 `ios_find_image` 核对，再 `ios_tap_image`。
4. **利用反馈图**：每次操作后直接检查返回图片，不额外截图。
5. **逐步执行**：导航、输入、按键分开调用，每一步都确认反馈图。
6. **显式选择设备**：多设备时每次传 `udid`，不要依赖连接顺序。

## 6. OMP 中验证与重载

用户级配置通常位于 `~/.omp/agent/mcp.json`，服务名为 `ios-control`。修改配置后在 OMP 中执行：

```text
/mcp reload
/mcp test ios-control
/mcp list
```

**预期结果：** 连接成功并发现 15 个工具。新 OMP 会话会自动加载该用户级配置。

## 7. 故障处理

| 现象 | 处理 |
|---|---|
| 没有检测到设备 | 检查 USB 数据线、信任状态和 `ios_list_devices`；运行 `pymobiledevice3 usbmux list` |
| 截图失败 | 解锁并亮屏；确认开发者模式和 DDI；根据返回的失败原因处理 |
| 点击成功但看不到反馈图 | 检查结果是否包含截图失败说明；不要重复点击，先确认设备实际状态 |
| OCR 找不到目标 | 用 `ios_describe` 查看实际识别文本；尝试更短子串，或改用模板匹配 |
| 模板匹配为空 | 从当前设备重新截模板；必要时逐步降低 `threshold`，同时检查误匹配 |
| 点击了错误匹配项 | 先调用查找工具检查 `count` 和坐标，再用明确的 `index` |
| 中文输入未进入目标 | 先点击输入框获得焦点；注意非 ASCII 输入会覆盖剪贴板 |
| MCP 调用超时 | 首次 tunnel/OCR 初始化较慢；保持设备解锁，并将客户端超时提高到 120000ms |

更多环境故障见 [README 的常见错误](README.md#常见错误)。

## 8. 安全注意事项

- 工具会真实操作 iPhone，没有 dry-run；点击、输入和按键前必须确认目标。
- 截图可能包含私人信息，只发送给可信 MCP 客户端。
- 虽然 server 不落盘，MCP 客户端仍可能按自身策略记录返回内容。
- 仅使用本机 stdio 或绑定 `127.0.0.1` 的 HTTP 服务，不要暴露到公网。
