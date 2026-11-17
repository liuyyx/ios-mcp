"""iOS 17+ 触摸 / 键盘 / 硬件按键注入。

实现方式
--------

iOS 17 起，HID 由开发者磁盘镜像里的 ``dtuhidd`` 提供，走 RemoteXPC：

* ``com.apple.coredevice.hid.universalhidservice``：触摸屏、虚拟键盘；
* ``com.apple.coredevice.hid.indigo``：Home / 音量 / 锁屏 / Siri 等硬件按键。

触摸和虚拟键盘受 backboardd 的鉴权限制：只有存在一条活动媒体流时，
``dtuhidd`` 才会把我们的 HID surface 标记为 authenticated，事件才会送达
UIKit。本模块通过 pymobiledevice3 的 :func:`touch_session` 保持这样一条
媒体流（画面数据直接丢弃）。

pymobiledevice3 11.57 的 ``DisplayService.start_video_stream`` 会以
“iOS 27 以下不发送视频帧”为由拒绝旧系统；但 dtuhidd 在 iOS 17–26 上仍会
接受启动请求并解除 HID 鉴权（只是没有画面）。因此本模块在 iOS < 27 时
临时关闭该版本检查，仅用于媒体流鉴权，不依赖视频帧。

坐标
----

``tap`` / ``swipe`` / ``scroll`` 接收**逻辑点**（例如 393×852），内部按
``ios_screen_size`` 的结果归一化到 0–65535。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Optional

from pymobiledevice3.remote.core_device.device_info import (
    DeviceInfoService,
    select_display,
)
from pymobiledevice3.remote.core_device.hid_service import (
    ASCII_TO_HID,
    HID_BUTTON_STATE_DOWN,
    HID_BUTTON_STATE_UP,
    KEY_LEFT_GUI,
    KEY_LEFT_SHIFT,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService,
    UniversalHIDServiceService,
    touch_session,
)
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)

from .device import IosMcpError, friendly_error, get_rsd
from .screen import screen_size

logger = logging.getLogger(__name__)

#: 一次手势结束后，多久自动关闭媒体流（秒）。0 表示保持到进程退出。
def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量，非法值时回退到默认值。"""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


HID_IDLE_SECONDS = _env_float("IOS_MCP_HID_IDLE_SECONDS", 120.0)
#: 建立 HID 会话（含媒体流鉴权）的超时。
HID_SESSION_TIMEOUT = 30.0
#: 触摸归一化坐标的最大值。
_HID_MAX = 65535
#: 单帧间隔（约 60fps）。
_FRAME_SECONDS = 1.0 / 60.0
#: 滚动时每次滑动的距离占屏幕短边的比例。
_SCROLL_DISTANCE_RATIO = 0.35

#: 逻辑按键名 -> (usage_page, usage_code, 按住时长)
_BUTTONS: dict[str, tuple[int, int, float]] = {
    "home": (0x0C, 0x40, 0.05),  # Consumer / Menu
    "lock": (0x0C, 0x30, 0.50),  # Consumer / Power
    "volume_up": (0x0C, 0xE9, 0.05),  # Consumer / Volume Increment
    "volume_down": (0x0C, 0xEA, 0.05),  # Consumer / Volume Decrement
    "siri": (0x0C, 0xCF, 1.00),  # Consumer / Voice Command
}

_KEY_ALIASES = {
    "volumeup": "volume_up",
    "volume-up": "volume_up",
    "vol_up": "volume_up",
    "vol-up": "volume_up",
    "volumedown": "volume_down",
    "volume-down": "volume_down",
    "vol_down": "volume_down",
    "vol-down": "volume_down",
    "power": "lock",
    "sleep": "lock",
    "voice": "siri",
}


def _ios_major(rsd: RemoteServiceDiscoveryService) -> int:
    """返回 iOS 主版本号；无法解析时返回 0。"""
    try:
        return int(str(rsd.product_version).split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


async def _display_id(rsd: RemoteServiceDiscoveryService) -> int:
    """取当前内置显示屏的 ``displayId``，失败时回退到 1。"""
    try:
        async with DeviceInfoService(rsd) as info:
            display = select_display(await info.get_display_info())
        return int(display.get("displayId") or 1)
    except Exception:  # noqa: BLE001 - 仅用于选择媒体流目标
        logger.debug("读取 displayId 失败，使用默认值 1", exc_info=True)
        return 1


@contextlib.asynccontextmanager
async def _hid_session(rsd: RemoteServiceDiscoveryService):
    """打开一个带媒体流鉴权的 Universal HID 会话。"""
    display_id = await _display_id(rsd)
    if _ios_major(rsd) >= 27:
        async with touch_session(rsd, display_id=display_id) as service:
            yield service
        return

    # iOS 17–26：dtuhidd 仍接受媒体流启动请求（用于 HID 鉴权），只是不发帧。
    # 临时跳过 pymobiledevice3 针对“视频帧”的版本检查。
    from pymobiledevice3.remote.core_device import display_service as _display_service

    original = _display_service.require_video_stream_support
    _display_service.require_video_stream_support = lambda *args, **kwargs: None
    try:
        logger.info("iOS %s < 27：为 HID 鉴权启动媒体流（忽略无视频帧的限制）", rsd.product_version)
        async with touch_session(rsd, display_id=display_id) as service:
            yield service
    finally:
        _display_service.require_video_stream_support = original


class HidController:
    """串行执行 HID 手势，并在空闲时释放媒体流。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session_cm: Optional[Any] = None
        self._service: Optional[UniversalHIDServiceService] = None
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._keyboard_id: Optional[int] = None
        self._idle_task: Optional[asyncio.Task[None]] = None
        self._idle_generation = 0

    async def close(self) -> None:
        """关闭 HID 会话和媒体流（可重复调用）。"""
        async with self._lock:
            await self._close_session_locked()

    async def _ensure_session_locked(
        self, rsd: RemoteServiceDiscoveryService
    ) -> UniversalHIDServiceService:
        """取得（必要时创建）与 *rsd* 绑定的 HID 会话。调用者需持锁。"""
        self._cancel_idle_locked()
        if self._service is not None and self._rsd is rsd:
            return self._service

        await self._close_session_locked()
        context_manager = _hid_session(rsd)
        try:
            service = await asyncio.wait_for(
                context_manager.__aenter__(), timeout=HID_SESSION_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await context_manager.__aexit__(None, None, None)
            message = friendly_error(exc)
            raise IosMcpError(
                "无法建立 HID 会话（媒体流鉴权失败）。请确认 iPhone 已解锁、"
                "开发者模式已开启；若屏幕镜像正被其它程序占用，请先关闭。"
                f"\n原始错误：{message}"
            ) from exc

        self._session_cm = context_manager
        self._service = service
        self._rsd = rsd
        self._keyboard_id = None
        logger.info("HID 会话已建立（设备 %s）", getattr(rsd, "udid", "?"))
        return service

    async def _close_session_locked(self) -> None:
        """关闭 HID 会话。调用者需持锁。"""
        task, self._idle_task = self._idle_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

        context_manager, self._session_cm = self._session_cm, None
        self._service = None
        self._rsd = None
        self._keyboard_id = None
        if context_manager is not None:
            with contextlib.suppress(Exception):
                await context_manager.__aexit__(None, None, None)

    def _cancel_idle_locked(self) -> None:
        """取消待执行的空闲关闭任务。调用者需持锁。"""
        self._idle_generation += 1
        task, self._idle_task = self._idle_task, None
        if task is not None:
            task.cancel()

    def _schedule_idle_close_locked(self) -> None:
        """安排空闲关闭。调用者需持锁。"""
        self._cancel_idle_locked()
        if HID_IDLE_SECONDS <= 0 or self._session_cm is None:
            return
        generation = self._idle_generation

        async def _closer() -> None:
            try:
                await asyncio.sleep(HID_IDLE_SECONDS)
            except asyncio.CancelledError:
                return
            async with self._lock:
                if generation == self._idle_generation:
                    await self._close_session_locked()

        self._idle_task = asyncio.create_task(_closer(), name="ios-mcp-hid-idle")

    async def _ensure_keyboard_locked(
        self, service: UniversalHIDServiceService
    ) -> int:
        """创建（并缓存）虚拟键盘服务。调用者需持锁。"""
        if self._keyboard_id is None:
            self._keyboard_id = await asyncio.wait_for(
                service.create_keyboard_service(), timeout=15
            )
        return self._keyboard_id

    @staticmethod
    def _normalise(
        x: float, y: float, width: int, height: int
    ) -> tuple[int, int]:
        """把逻辑点坐标转换成 HID 的 0–65535 归一化坐标。"""
        if width <= 0 or height <= 0:
            raise IosMcpError(f"无效的屏幕尺寸：{width}x{height}")
        if not (0 <= x <= width and 0 <= y <= height):
            raise IosMcpError(
                f"坐标 ({x}, {y}) 超出逻辑分辨率 {width}x{height}。"
                "请先用 `ios_screen_size` 确认尺寸。"
            )
        hid_x = round(x / width * _HID_MAX)
        hid_y = round(y / height * _HID_MAX)
        return min(max(hid_x, 0), _HID_MAX), min(max(hid_y, 0), _HID_MAX)

    async def tap(self, x: int, y: int, udid: str = "") -> None:
        """在逻辑坐标 (x, y) 点击一次。"""
        rsd = await get_rsd(udid)
        width, height = await screen_size(udid)
        hid_x, hid_y = self._normalise(x, y, width, height)

        async with self._lock:
            service = await self._ensure_session_locked(rsd)
            try:
                await service.send_touchscreen(
                    TOUCHSCREEN_STATE_CONTACT, hid_x, hid_y
                )
                await asyncio.sleep(0.05)
                await service.send_touchscreen(
                    TOUCHSCREEN_STATE_RELEASE, hid_x, hid_y
                )
            finally:
                self._schedule_idle_close_locked()

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
        udid: str = "",
    ) -> None:
        """从 (x1, y1) 滑到 (x2, y2)，默认耗时 300ms。"""
        rsd = await get_rsd(udid)
        width, height = await screen_size(udid)
        start = self._normalise(x1, y1, width, height)
        end = self._normalise(x2, y2, width, height)
        duration_ms = max(50, min(int(duration_ms), 10_000))

        async with self._lock:
            service = await self._ensure_session_locked(rsd)
            try:
                await self._swipe_locked(service, start, end, duration_ms)
            finally:
                self._schedule_idle_close_locked()

    async def _swipe_locked(
        self,
        service: UniversalHIDServiceService,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        """在持锁状态下执行一次滑动。"""
        steps = max(1, min(240, round(duration_ms / 16)))
        step_delay = duration_ms / 1000 / steps
        await service.send_touchscreen(
            TOUCHSCREEN_STATE_CONTACT, start[0], start[1]
        )
        for index in range(1, steps + 1):
            ratio = index / steps
            x = round(start[0] + (end[0] - start[0]) * ratio)
            y = round(start[1] + (end[1] - start[1]) * ratio)
            await service.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
            await asyncio.sleep(step_delay)
        await service.send_touchscreen(
            TOUCHSCREEN_STATE_RELEASE, end[0], end[1]
        )

    async def scroll(self, direction: str, amount: int = 1, udid: str = "") -> None:
        """按方向滚动内容。

        :param direction: ``up`` / ``down`` / ``left`` / ``right``。
        :param amount: 连续滑动的次数，1–10。
        """
        direction = direction.strip().lower()
        aliases = {
            "上": "up",
            "下": "down",
            "左": "left",
            "右": "right",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
        }
        direction = aliases.get(direction, direction)
        if direction not in ("up", "down", "left", "right"):
            raise IosMcpError("direction 必须是 up / down / left / right 之一。")
        amount = max(1, min(int(amount), 10))

        rsd = await get_rsd(udid)
        width, height = await screen_size(udid)
        distance = round(min(width, height) * _SCROLL_DISTANCE_RATIO)
        half = round(distance / 2)

        offsets = {
            "up": (0, half, 0, -half),
            "down": (0, -half, 0, half),
            "left": (half, 0, -half, 0),
            "right": (-half, 0, half, 0),
        }
        dx1, dy1, dx2, dy2 = offsets[direction]
        start = self._normalise(
            width / 2 + dx1, height / 2 + dy1, width, height
        )
        end = self._normalise(width / 2 + dx2, height / 2 + dy2, width, height)

        async with self._lock:
            service = await self._ensure_session_locked(rsd)
            try:
                for index in range(amount):
                    await self._swipe_locked(service, start, end, 250)
                    if index + 1 < amount:
                        await asyncio.sleep(0.08)
            finally:
                self._schedule_idle_close_locked()

    async def input_text(self, text: str, udid: str = "") -> None:
        """向当前焦点输入文本。

        ASCII 字符通过虚拟键盘逐字输入；包含非 ASCII 字符（如中文）时，
        改为写入设备剪贴板并模拟 Cmd+V。使用剪贴板方案会覆盖 iPhone 上
        原有的剪贴板内容。
        """
        if not text:
            return
        rsd = await get_rsd(udid)
        ascii_only = all(char in ASCII_TO_HID for char in text)

        async with self._lock:
            service = await self._ensure_session_locked(rsd)
            try:
                keyboard_id = await self._ensure_keyboard_locked(service)
                if ascii_only:
                    for char in text:
                        usage, shift = ASCII_TO_HID[char]
                        keys = [usage]
                        if shift:
                            keys.append(KEY_LEFT_SHIFT)
                        await service.send_keyboard(keyboard_id, keys)
                        await asyncio.sleep(0.015)
                        await service.send_keyboard(keyboard_id, ())
                        await asyncio.sleep(0.01)
                else:
                    await self._paste_text(rsd, service, keyboard_id, text)
            finally:
                self._schedule_idle_close_locked()

    async def _paste_text(
        self,
        rsd: RemoteServiceDiscoveryService,
        service: UniversalHIDServiceService,
        keyboard_id: int,
        text: str,
    ) -> None:
        """写入设备剪贴板并发送 Cmd+V。"""
        from pymobiledevice3.remote.core_device.pasteboard_service import (
            PasteboardService,
        )

        try:
            async with PasteboardService(rsd) as pasteboard:
                await asyncio.wait_for(pasteboard.set_text(text), timeout=10)
        except Exception as exc:  # noqa: BLE001
            raise IosMcpError(
                "文本包含非 ASCII 字符，需要走剪贴板粘贴，但写入设备剪贴板失败："
                f"{friendly_error(exc)}"
            ) from exc

        await asyncio.sleep(0.15)
        v_usage = ASCII_TO_HID["v"][0]
        await service.send_keyboard(keyboard_id, (KEY_LEFT_GUI,))
        await asyncio.sleep(0.02)
        await service.send_keyboard(keyboard_id, (KEY_LEFT_GUI, v_usage))
        await asyncio.sleep(0.08)
        await service.send_keyboard(keyboard_id, ())

    async def press_key(self, key: str, udid: str = "") -> None:
        """按下并松开一个硬件按键。

        :param key: ``home`` / ``volume_up`` / ``volume_down`` / ``lock`` / ``siri``。
        """
        normalised = _KEY_ALIASES.get(key.strip().lower(), key.strip().lower())
        if normalised not in _BUTTONS:
            raise IosMcpError(
                f"不支持的按键 {key!r}。可用：home、volume_up、volume_down、lock、siri。"
            )
        usage_page, usage_code, hold = _BUTTONS[normalised]
        rsd = await get_rsd(udid)

        async with self._lock:
            async with IndigoHIDService(rsd) as service:
                await service.send_button(
                    usage_page, usage_code, HID_BUTTON_STATE_DOWN
                )
                await asyncio.sleep(hold)
                await service.send_button(
                    usage_page, usage_code, HID_BUTTON_STATE_UP
                )
                # dtuhidd 异步派发消息，稍等片刻再断开以免最后一个包被丢弃。
                await asyncio.sleep(0.1)


#: 全进程共享的 HID 控制器。
hid = HidController()
