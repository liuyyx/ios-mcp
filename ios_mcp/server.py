"""MCP server 入口：把 iPhone 控制能力暴露成 MCP 工具。

运行方式::

    python -m ios_mcp.server                     # stdio（Claude Desktop / Cursor）
    python -m ios_mcp.server --transport sse     # HTTP/SSE，默认 127.0.0.1:8000

也可以安装后直接运行 ``ios-mcp``。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any, NoReturn, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as _FastMCPSettings

from . import __version__
from .device import friendly_error, list_ios_devices, manager
from .hid import hid
from .screen import screenshot_base64, screen_size

# mcp 1.26.0 的 Settings.lifespan 含未解析的前向引用，传入 lifespan 时会触发
# pydantic-settings 的 IncompleteFieldDefinitionWarning。提前重建模型消除噪音。
_FastMCPSettings.model_rebuild()

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
这个 server 通过 USB 控制一台已配对、已解锁的 iPhone（iOS 17+）。

- 坐标使用逻辑点（例如 iPhone 15 Pro 是 393x852），不是截图的物理像素。
  不确定时先调用 ios_screen_size。
- 截图返回 base64 PNG（物理像素）。
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
async def ios_screenshot(udid: str = "") -> str:
    """截取 iPhone 当前屏幕。

    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    :returns: base64 编码的 PNG（不带 data: 前缀），内容是物理像素分辨率。
    """
    try:
        return await screenshot_base64(udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@mcp.tool()
async def ios_tap(x: int, y: int, udid: str = "") -> str:
    """在屏幕的逻辑坐标 (x, y) 处点击一次（按下 50ms 后抬起）。

    :param x: 逻辑点 X 坐标（0 到屏幕宽度）。
    :param y: 逻辑点 Y 坐标（0 到屏幕高度）。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        await hid.tap(x, y, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return f"✅ 点击成功：({x}, {y})"


@mcp.tool()
async def ios_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
    udid: str = "",
) -> str:
    """从 (x1, y1) 滑动到 (x2, y2)，可用于拖动、翻页、解锁等。

    :param x1: 起点逻辑 X 坐标。
    :param y1: 起点逻辑 Y 坐标。
    :param x2: 终点逻辑 X 坐标。
    :param y2: 终点逻辑 Y 坐标。
    :param duration_ms: 滑动耗时（毫秒），默认 300，范围 50–10000。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        await hid.swipe(x1, y1, x2, y2, duration_ms, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return f"✅ 滑动成功：({x1}, {y1}) → ({x2}, {y2})，{duration_ms}ms"


@mcp.tool()
async def ios_scroll(
    direction: str, amount: int = 1, udid: str = ""
) -> str:
    """从屏幕中心向指定方向滑动，滚动页面内容。

    :param direction: up / down / left / right（也接受 上/下/左/右）。
    :param amount: 连续滑动次数，1–10，默认 1。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        await hid.scroll(direction, amount, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return f"✅ 滚动成功：{direction} × {amount}"


@mcp.tool()
async def ios_input_text(text: str, udid: str = "") -> str:
    """向当前焦点输入文本。

    ASCII 字符通过虚拟键盘输入；包含中文等非 ASCII 字符时，会写入 iPhone
    剪贴板并模拟 Cmd+V（会覆盖设备原剪贴板内容）。

    :param text: 要输入的文本。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        await hid.input_text(text, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return f"✅ 已输入 {len(text)} 个字符"


@mcp.tool()
async def ios_press_key(key: str, udid: str = "") -> str:
    """按下并松开一个 iPhone 硬件按键。

    :param key: home / volume_up / volume_down / lock / siri。
    :param udid: 目标设备 UDID；留空表示第一台 USB 设备。
    """
    try:
        await hid.press_key(key, udid)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    return f"✅ 已按下按键：{key}"


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
