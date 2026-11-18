"""MCP server 入口：把 iPhone 控制能力暴露成 MCP 工具。

运行方式::

    python -m ios_mcp.server                     # stdio（Claude Desktop / Cursor）
    python -m ios_mcp.server --transport sse     # HTTP/SSE，默认 127.0.0.1:8000

也可以安装后直接运行 ``ios-mcp``。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any, NoReturn, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as _FastMCPSettings
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

from . import __version__, vision
from .device import IosMcpError, friendly_error, list_ios_devices, manager
from .hid import hid
from .screen import capture_screenshot, screenshot_base64, screen_size

# mcp 1.26.0 的 Settings.lifespan 含未解析的前向引用，传入 lifespan 时会触发
# pydantic-settings 的 IncompleteFieldDefinitionWarning。提前重建模型消除噪音。
_FastMCPSettings.model_rebuild()

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
这个 server 通过 USB 控制一台已配对、已解锁的 iPhone（iOS 17+）。

- 工具参数使用逻辑点（ios_screen_size 可查），截图使用物理像素；OCR 和
  模板匹配结果里的 point 已换算成逻辑点，可直接用于点击。
- 点击、滑动、滚动、文本输入和按键工具会在操作后自动截图，并随成功消息
  一起返回，可直接确认界面变化。
- 定位优先调用 ios_describe 或 ios_find_text；主屏图标可用
  ios_tap_text(label, offset_y=-48) 点击文字上方的图标。无文字图标先调用
  ios_find_image 确认位置，再调用 ios_tap_image。
- 触摸/键盘注入需要开发者模式；首次调用会建立媒体流以通过 HID 鉴权，
  可能比后续调用慢 1–3 秒。
- 若设备锁屏，请先解锁：锁屏状态下截图和触摸都会失败。
"""


@contextlib.asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """进程退出时关闭 HID 媒体流和 RSD tunnel。"""
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await hid.close()
        with contextlib.suppress(Exception):
            await manager.close()


mcp = FastMCP("ios-control", instructions=INSTRUCTIONS, lifespan=_lifespan)


def _fail(exc: BaseException) -> NoReturn:
    """把底层异常转换成带操作建议的 MCP 工具错误。"""
    error = friendly_error(exc)
    raise error from exc


def _encode_image(png: bytes, image_format: str, quality: int) -> tuple[bytes, str]:
    """按需把 PNG 截图编码为 JPEG；转换失败时保留原 PNG。"""
    if image_format == "jpeg" and png.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            with Image.open(io.BytesIO(png)) as image:
                output = io.BytesIO()
                image.convert("RGB").save(
                    output, format="JPEG", quality=quality, optimize=True
                )
                return output.getvalue(), "image/jpeg"
        except Exception:  # noqa: BLE001
            logger.debug("JPEG 转换失败，改为返回 PNG", exc_info=True)
    return png, "image/png"


async def _action_result(
    message: str,
    udid: str,
    settle_ms: int,
    image_format: str = "jpeg",
    quality: int = 85,
) -> CallToolResult:
    """等待界面稳定后，把操作成功消息和反馈截图一起返回。"""
    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000)
    try:
        png = await capture_screenshot(udid)
    except Exception as exc:  # noqa: BLE001 - 截图失败不能抹掉已成功的操作
        summary = str(friendly_error(exc)).strip().splitlines()[0]
        return CallToolResult(
            content=[
                TextContent(type="text", text=f"{message}\n（截图失败：{summary}）")
            ]
        )

    encoded, mime_type = _encode_image(png, image_format, quality)
    return CallToolResult(
        content=[
            TextContent(type="text", text=message),
            ImageContent(
                type="image",
                data=base64.b64encode(encoded).decode("ascii"),
                mimeType=mime_type,
            ),
        ]
    )


@mcp.tool()
async def ios_list_devices() -> list[dict[str, Any]]:
    """列出通过 USB 连接的 iPhone。

    返回 JSON 数组，每项包含：
    - udid：设备唯一标识
    - name：设备名称（未解锁/未信任时可能是占位名称）
    - ios_version：iOS 版本，例如 "18.2"
    - connected：当前是否连接
    """
    try:
        return await list_ios_devices()
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_screenshot(udid: str = "", image_format: str = "png") -> CallToolResult:
    """截取 iPhone 当前屏幕，返回可渲染图片和物理像素尺寸。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param image_format: png（无损，默认）或 jpeg（体积更小）。
    """
    try:
        png = await capture_screenshot(udid)
        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
        encoded, mime_type = _encode_image(png, image_format, 85)
        return CallToolResult(
            content=[
                TextContent(
                    type="text", text=f"✅ 截图成功：{width} × {height} 物理像素"
                ),
                ImageContent(
                    type="image",
                    data=base64.b64encode(encoded).decode("ascii"),
                    mimeType=mime_type,
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_screenshot_base64(udid: str = "") -> str:
    """需要原始数据时，截取屏幕并返回纯 base64 PNG（不带 data: 前缀）。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        return await screenshot_base64(udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_tap(
    x: int, y: int, udid: str = "", settle_ms: int = 600
) -> CallToolResult:
    """在屏幕的逻辑坐标 (x, y) 处点击一次，并返回操作后的截图。

    :param x: 逻辑点 X 坐标（0 到屏幕宽度）。
    :param y: 逻辑点 Y 坐标（0 到屏幕高度）。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        await hid.tap(x, y, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(f"✅ 点击成功：({x}, {y})", udid, settle_ms)


@mcp.tool()
async def ios_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
    udid: str = "",
    settle_ms: int = 600,
) -> CallToolResult:
    """从 (x1, y1) 滑动到 (x2, y2)，并返回操作后的截图。

    :param x1: 起点逻辑 X 坐标。
    :param y1: 起点逻辑 Y 坐标。
    :param x2: 终点逻辑 X 坐标。
    :param y2: 终点逻辑 Y 坐标。
    :param duration_ms: 滑动耗时（毫秒），默认 300，范围 50–10000。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        await hid.swipe(x1, y1, x2, y2, duration_ms, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(
        f"✅ 滑动成功：({x1}, {y1}) → ({x2}, {y2})，{duration_ms}ms",
        udid,
        settle_ms,
    )


@mcp.tool()
async def ios_scroll(
    direction: str, amount: int = 1, udid: str = "", settle_ms: int = 600
) -> CallToolResult:
    """从屏幕中心向指定方向滑动，滚动页面内容，并返回操作后的截图。

    :param direction: up / down / left / right（也接受 上/下/左/右）。
    :param amount: 连续滑动次数，1–10，默认 1。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        await hid.scroll(direction, amount, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(f"✅ 滚动成功：{direction} × {amount}", udid, settle_ms)


@mcp.tool()
async def ios_input_text(
    text: str, udid: str = "", settle_ms: int = 600
) -> CallToolResult:
    """向当前焦点输入文本，并返回操作后的截图。

    ASCII 字符通过虚拟键盘输入；包含中文等非 ASCII 字符时，会写入 iPhone
    剪贴板并模拟 Cmd+V（会覆盖设备原剪贴板内容）。

    :param text: 要输入的文本。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        await hid.input_text(text, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(f"✅ 已输入 {len(text)} 个字符", udid, settle_ms)


@mcp.tool()
async def ios_press_key(
    key: str, udid: str = "", settle_ms: int = 600
) -> CallToolResult:
    """按下并松开一个 iPhone 硬件按键，并返回操作后的截图。

    :param key: home / volume_up / volume_down / lock / siri。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        await hid.press_key(key, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(f"✅ 已按下按键：{key}", udid, settle_ms)


@mcp.tool()
async def ios_ocr(udid: str = "") -> dict[str, Any]:
    """识别当前屏幕文本，返回像素框和可直接点击的逻辑点坐标。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        return await vision.ocr_screen_payload(udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_find_text(
    text: str, udid: str = "", exact: bool = False
) -> dict[str, Any]:
    """在当前屏幕查找文本并返回匹配位置。

    :param text: 要查找的文本。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param exact: True 表示完整匹配；False 表示大小写不敏感的子串匹配。
    """
    try:
        return await vision.find_text_payload(text, udid, exact)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_tap_text(
    text: str,
    udid: str = "",
    exact: bool = False,
    index: int = 0,
    offset_y: int = 0,
    settle_ms: int = 600,
) -> CallToolResult:
    """找到屏幕文本并点击其中心，然后返回操作后的截图。

    主屏图标通常可传 offset_y=-48，点击图标名称上方的图标本体。

    :param text: 要查找并点击的文本。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param exact: True 表示完整匹配；False 表示大小写不敏感的子串匹配。
    :param index: 多个匹配项中的序号，从 0 开始。
    :param offset_y: 相对文本中心的逻辑点纵向偏移，主屏图标可用 -48。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        payload = await vision.find_text_payload(text, udid, exact)
        count = payload["count"]
        if count == 0:
            screen = await vision.ocr_screen_payload(udid)
            recognized = "、".join(
                item["text"] for item in screen["texts"][:5]
            ) or "无"
            raise IosMcpError(
                f"未找到文本 {text!r}；当前界面识别到：{recognized}"
            )
        if not 0 <= index < count:
            raise IosMcpError(
                f"文本 {text!r} 共有 {count} 个匹配项，index={index} 越界。"
            )
        point = payload["matches"][index]["point"]
        tap_x, tap_y = point["x"], point["y"] + offset_y
        await hid.tap(tap_x, tap_y, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(
        f"✅ 已点击文本「{text}」：({tap_x}, {tap_y})", udid, settle_ms
    )


@mcp.tool()
async def ios_find_image(
    template_base64: str, udid: str = "", threshold: float = 0.8
) -> dict[str, Any]:
    """用模板图在当前屏幕做图像匹配，返回像素框和逻辑点坐标。

    :param template_base64: 模板 PNG/JPEG 的 base64，可带 data: 前缀。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param threshold: 匹配阈值，范围建议 0–1，默认 0.8。
    """
    try:
        return await vision.find_image_payload(template_base64, udid, threshold)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_tap_image(
    template_base64: str,
    udid: str = "",
    threshold: float = 0.8,
    index: int = 0,
    settle_ms: int = 600,
) -> CallToolResult:
    """找到与模板匹配的图像并点击其中心，然后返回操作后的截图。

    :param template_base64: 模板 PNG/JPEG 的 base64，可带 data: 前缀。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param threshold: 匹配阈值，范围建议 0–1，默认 0.8。
    :param index: 多个匹配项中的序号，从 0 开始。
    :param settle_ms: 操作后等待 UI 动画再截图的毫秒数，0 表示立即截图。
    """
    try:
        payload = await vision.find_image_payload(template_base64, udid, threshold)
        count = payload["count"]
        if count == 0:
            raise IosMcpError(f"未找到模板图像（threshold={threshold}）。")
        if not 0 <= index < count:
            raise IosMcpError(
                f"模板图像共有 {count} 个匹配项，index={index} 越界。"
            )
        point = payload["matches"][index]["point"]
        tap_x, tap_y = point["x"], point["y"]
        await hid.tap(tap_x, tap_y, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return await _action_result(
        f"✅ 已点击模板图像：({tap_x}, {tap_y})", udid, settle_ms
    )


@mcp.tool()
async def ios_describe(udid: str = "", max_texts: int = 80) -> CallToolResult:
    """返回同一张屏幕截图及其中的 OCR 文本、像素框和逻辑点坐标。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :param max_texts: 最多返回的文本条数，默认 80；0 表示只返回图片。
    """
    try:
        png = await capture_screenshot(udid)
        hits, pixel_size = await vision.ocr_bytes(png)
        point_size = await screen_size(udid)
        items = [hit.to_payload(pixel_size, point_size) for hit in hits]
        limit = max(0, max_texts)
        payload = {
            "pixel_size": {"width": pixel_size[0], "height": pixel_size[1]},
            "point_size": {"width": point_size[0], "height": point_size[1]},
            "count": len(items),
            "texts": items[:limit],
            "truncated": len(items) > limit,
        }
        return CallToolResult(
            content=[
                TextContent(
                    type="text", text=json.dumps(payload, ensure_ascii=False)
                ),
                ImageContent(
                    type="image",
                    data=base64.b64encode(png).decode("ascii"),
                    mimeType="image/png",
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_screen_size(udid: str = "") -> dict[str, int]:
    """返回 iPhone 的逻辑分辨率（点），用于换算 ios_tap / ios_swipe 的坐标。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :returns: ``{"width": 393, "height": 852}`` 形式的字典。
    """
    try:
        width, height = await screen_size(udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return {"width": width, "height": height}


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="ios-mcp",
        description="通过 pymobiledevice3 控制 iPhone（iOS 17+，USB）的 MCP server。",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.getenv("IOS_MCP_TRANSPORT", "stdio"),
        help="MCP 传输方式，默认 stdio（Claude Desktop / Cursor 使用）。",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("IOS_MCP_HOST", "127.0.0.1"),
        help="SSE / HTTP 传输监听地址，默认 127.0.0.1。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("IOS_MCP_PORT", "8000")),
        help="SSE / HTTP 传输端口，默认 8000。",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("IOS_MCP_LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="日志级别（输出到 stderr），默认 INFO。",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    """解析参数并启动 MCP server。"""
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if args.transport == "stdio":
        logger.info("ios-mcp %s 已启动（stdio）", __version__)
    else:
        logger.info(
            "ios-mcp %s 已启动（%s，http://%s:%s）",
            __version__,
            args.transport,
            args.host,
            args.port,
        )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
