"""iPhone 截图与逻辑分辨率查询。

截图按以下顺序尝试：

1. CoreDevice ``com.apple.coredevice.screencaptureservice``（iOS 17+，RSD tunnel）；
2. DVT ``com.apple.instruments.server.services.screenshot``（开发者镜像）；
3. lockdown ``com.apple.mobile.screenshotr``（需要已挂载开发者镜像）。

最终始终返回真正的 PNG（必要时用 Pillow 重新编码）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
from typing import Any, Optional

from PIL import Image
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.core_device.device_info import (
    DeviceInfoService,
    select_display,
)
from pymobiledevice3.remote.core_device.screen_capture_service import (
    ScreenCaptureService,
)
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
from pymobiledevice3.services.screenshot import ScreenshotService

from .device import (
    DEVELOPER_HINT,
    IosMcpError,
    friendly_error,
    get_rsd,
    resolve_udid,
)

logger = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SCREENSHOT_TIMEOUT = 20.0
_DISPLAY_INFO_TIMEOUT = 10.0

#: UDID -> 逻辑分辨率（点）。屏幕尺寸在运行期间不会变化，直接缓存。
_screen_size_cache: dict[str, tuple[int, int]] = {}


def _extract_image_bytes(result: Any) -> bytes:
    """从不同截图 API 的返回值中取出图片字节。"""
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    if isinstance(result, dict):
        for key in ("image", "data", "screenshot", "png"):
            value = result.get(key)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            if isinstance(value, str):
                return base64.b64decode(value)
    raise IosMcpError(f"截图服务返回了无法识别的数据：{type(result).__name__}")


def _ensure_png(data: bytes) -> bytes:
    """确保返回的是 PNG；其它格式（TIFF/JPEG）用 Pillow 转成 PNG。"""
    if not data:
        raise IosMcpError("截图服务返回了空数据。")
    if data.startswith(_PNG_MAGIC):
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise IosMcpError(f"无法把设备返回的截图解码为 PNG：{exc}") from exc


async def _capture_via_core_device(rsd: RemoteServiceDiscoveryService) -> bytes:
    """使用 CoreDevice screencaptureservice 截图。"""
    async with ScreenCaptureService(rsd) as service:
        result = await asyncio.wait_for(
            service.capture_screenshot(requested_format="png"),
            timeout=_SCREENSHOT_TIMEOUT,
        )
    return _ensure_png(_extract_image_bytes(result))


async def _capture_via_dvt(rsd: RemoteServiceDiscoveryService) -> bytes:
    """使用 DVT Instruments 的 Screenshot 服务截图。"""
    provider = DvtProvider(rsd)
    try:
        async with Screenshot(provider) as screenshot:
            data = await asyncio.wait_for(
                screenshot.get_screenshot(), timeout=_SCREENSHOT_TIMEOUT
            )
    finally:
        with contextlib.suppress(Exception):
            await provider.close()
    return _ensure_png(data)


async def _capture_via_lockdown(udid: str) -> bytes:
    """退化方案：通过 lockdown 的 screenshotr 服务截图。"""
    target = await resolve_udid(udid)
    lockdown = await asyncio.wait_for(
        create_using_usbmux(serial=target), timeout=_SCREENSHOT_TIMEOUT
    )
    try:
        async with ScreenshotService(lockdown) as service:
            data = await asyncio.wait_for(
                service.take_screenshot(), timeout=_SCREENSHOT_TIMEOUT
            )
    finally:
        with contextlib.suppress(Exception):
            await lockdown.close()
    return _ensure_png(data)


async def capture_screenshot(udid: str = "") -> bytes:
    """截取 iPhone 当前屏幕，返回 PNG 字节。

    :param udid: 目标设备；为空时自动选择。
    :raises IosMcpError: 所有截图路径都失败时，错误信息会包含每一步的原因。
    """
    errors: list[str] = []

    rsd: Optional[RemoteServiceDiscoveryService] = None
    try:
        rsd = await get_rsd(udid)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"RSD tunnel：{friendly_error(exc)}")

    if rsd is not None:
        try:
            return await _capture_via_core_device(rsd)
        except Exception as exc:  # noqa: BLE001
            logger.debug("CoreDevice 截图失败", exc_info=True)
            errors.append(f"CoreDevice screencaptureservice：{friendly_error(exc)}")
        try:
            return await _capture_via_dvt(rsd)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DVT 截图失败", exc_info=True)
            errors.append(f"DVT screenshot：{friendly_error(exc)}")

    try:
        return await _capture_via_lockdown(udid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("lockdown 截图失败", exc_info=True)
        errors.append(f"lockdown screenshotr：{friendly_error(exc)}")

    detail = "\n  - ".join(errors)
    raise IosMcpError(
        "截图失败。请先解锁 iPhone 并保持亮屏；然后确认已开启开发者模式、"
        f"已挂载开发者镜像。{DEVELOPER_HINT}\n各路径的错误：\n  - {detail}"
    )


async def screenshot_base64(udid: str = "") -> str:
    """截取屏幕并返回 base64 编码的 PNG（不带 ``data:`` 前缀）。"""
    png = await capture_screenshot(udid)
    return base64.b64encode(png).decode("ascii")


def _size_from_mapping(mapping: Any) -> Optional[tuple[float, float]]:
    """从 display info 的各种字段中解析出 (宽, 高)。"""
    if not isinstance(mapping, dict):
        return None
    for key in ("size", "pointSize", "pixelSize", "logicalSize"):
        value = mapping.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                width, height = float(value[0]), float(value[1])
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                return width, height
        if isinstance(value, dict):
            parsed = _size_from_mapping(value)
            if parsed is not None:
                return parsed
    width = mapping.get("width")
    height = mapping.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        if width > 0 and height > 0:
            return float(width), float(height)
    return None


def _number(value: Any) -> Optional[float]:
    """把 ``3`` / ``3.0`` / ``"3"`` / ``"3x"`` 统一成 float。"""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.rstrip("xX"))
            return parsed if parsed > 0 else None
        except ValueError:
            return None
    return None


def _guess_scale(pixel_width: int, product_type: str = "") -> float:
    """在设备没报告缩放倍率时，按 iPhone/iPad 常见型号猜一个。"""
    if product_type.lower().startswith("ipad"):
        return 2.0
    # iOS 17+ 的 iPhone 中，只有 iPhone XR / 11 / SE 使用 @2x。
    if pixel_width <= 900:
        return 2.0
    return 3.0


def _logical_size_from_display_info(
    display_info: dict[str, Any], product_type: str = ""
) -> Optional[tuple[int, int]]:
    """从 ``getdisplayinfo`` 的返回值推导逻辑分辨率（点）。"""
    display = select_display(display_info)
    current_mode = display.get("currentMode") or {}

    # 1) 设备直接给出“点”尺寸时最可靠。
    for container in (display, current_mode):
        for key in ("pointSize", "logicalSize", "pointWidth", "pointHeight"):
            if key == "pointWidth":
                width = _number(container.get("pointWidth"))
                height = _number(container.get("pointHeight"))
                if width and height:
                    return round(width), round(height)
            else:
                parsed = _size_from_mapping({key: container.get(key)})
                if parsed is not None:
                    return round(parsed[0]), round(parsed[1])

    # 2) 像素尺寸 + 缩放倍率。
    pixels = _size_from_mapping(current_mode) or _size_from_mapping(display)
    if pixels is None:
        return None
    scale = None
    for container in (display, current_mode):
        for key in ("scale", "nativeScale", "pixelScale", "displayScale"):
            scale = _number(container.get(key))
            if scale and scale >= 1:
                break
        if scale and scale >= 1:
            break
    if not scale or scale < 1:
        scale = _guess_scale(round(pixels[0]), product_type)
    return round(pixels[0] / scale), round(pixels[1] / scale)


def _logical_size_from_png(png: bytes, product_type: str = "") -> tuple[int, int]:
    """退化方案：用截图像素尺寸和常见缩放倍率推算逻辑分辨率。"""
    with Image.open(io.BytesIO(png)) as image:
        width, height = image.size
    scale = _guess_scale(width, product_type)
    return round(width / scale), round(height / scale)


async def screen_size(udid: str = "") -> tuple[int, int]:
    """返回 iPhone 的逻辑分辨率（点），例如 iPhone 15 Pro 为 ``(393, 852)``。

    :param udid: 目标设备；为空时自动选择。
    :raises IosMcpError: 无法从设备或截图获得尺寸时。
    """
    target = await resolve_udid(udid)
    cached = _screen_size_cache.get(target)
    if cached is not None:
        return cached

    errors: list[str] = []
    product_type = ""
    rsd: Optional[RemoteServiceDiscoveryService] = None
    try:
        rsd = await get_rsd(target)
        product_type = getattr(rsd, "product_type", "") or ""
        async with DeviceInfoService(rsd) as info:
            display_info = await asyncio.wait_for(
                info.get_display_info(), timeout=_DISPLAY_INFO_TIMEOUT
            )
        size = _logical_size_from_display_info(display_info, product_type)
        if size is not None:
            _screen_size_cache[target] = size
            return size
        errors.append("getdisplayinfo 没有返回可用的宽高")
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 display info 失败", exc_info=True)
        errors.append(f"getdisplayinfo：{friendly_error(exc)}")

    try:
        png = await capture_screenshot(target)
        size = _logical_size_from_png(png, product_type)
        _screen_size_cache[target] = size
        return size
    except Exception as exc:  # noqa: BLE001
        errors.append(f"从截图推算：{friendly_error(exc)}")

    raise IosMcpError(
        "无法获取 iPhone 屏幕尺寸。\n  - " + "\n  - ".join(errors)
    )


def clear_screen_size_cache() -> None:
    """清除逻辑分辨率缓存（例如设备方向/显示模式变化后）。"""
    _screen_size_cache.clear()
