"""设备发现、lockdown 信息查询与 iOS 17+ RSD tunnel 管理。

iOS 17 起，截图、HID 等开发者服务不再通过 lockdownd 的 ``StartService``
启动，而是走 RemoteServiceDiscovery（RSD）tunnel。本模块默认使用
pymobiledevice3 的用户态 tunnel（不需要 root），也支持连接已运行的
``tunneld``。

环境变量：

* ``IOS_MCP_UDID``      未传 udid 时使用的默认设备。
* ``IOS_MCP_TUNNEL``    ``userspace``（默认）或 ``tunneld``。
* ``IOS_MCP_TUNNELD``   已运行的 tunneld 地址，默认 ``127.0.0.1:49151``。
* ``IOS_MCP_RSD``       直接连接已有 RSD，格式 ``host:port``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from pymobiledevice3.exceptions import (
    ConnectionFailedToUsbmuxdError,
    DeveloperDiskImageServiceError,
    DeviceVersionNotSupportedError,
    IncorrectModeError,
    InvalidServiceError,
    MuxException,
    NoDeviceConnectedError,
    NotPairedError,
    PyMobileDevice3Exception,
    StartServiceError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)
from pymobiledevice3.usbmux import MuxDevice, list_devices

logger = logging.getLogger(__name__)

DEFAULT_TUNNELD_ADDRESS = ("127.0.0.1", 49151)
TUNNEL_OPEN_TIMEOUT = 60.0
LOCKDOWN_TIMEOUT = 20.0

TRUST_HINT = (
    "请解锁 iPhone，在屏幕上弹出的“要信任此电脑吗？”对话框里点“信任”，"
    "然后输入锁屏密码。"
)
DEVELOPER_HINT = (
    "请确认：1) iPhone 上已打开 设置 > 隐私与安全性 > 开发者模式 并重启过设备；"
    "2) 已连接 Xcode 或运行 `pymobiledevice3 mounter auto-mount` 挂载开发者磁盘镜像；"
    "3) iPhone 处于解锁、亮屏状态。"
)


class IosMcpError(RuntimeError):
    """面向使用者的、带可操作提示的错误。"""


def friendly_error(exc: BaseException) -> IosMcpError:
    """把一个底层异常转换成能看懂的、带处理建议的 :class:`IosMcpError`。

    已经是 :class:`IosMcpError` 的原样返回，避免重复包装。
    """
    if isinstance(exc, IosMcpError):
        return exc

    if isinstance(exc, NotPairedError):
        return IosMcpError(f"iPhone 尚未信任这台电脑。{TRUST_HINT}")
    if isinstance(exc, ConnectionFailedToUsbmuxdError):
        return IosMcpError(
            "连接不到 usbmuxd（USB 设备守护进程）。macOS 请安装 Apple Devices，"
            "Linux 请安装 usbmuxd / libimobiledevice，然后重新插拔数据线；"
            "也可以运行 `pymobiledevice3 doctor` 自检。"
        )
    if isinstance(exc, NoDeviceConnectedError):
        return IosMcpError(f"没有检测到 iPhone。请用数据线连接并解锁手机。{TRUST_HINT}")
    if isinstance(exc, MuxException):
        return IosMcpError(
            "usbmuxd 报告设备异常（设备可能刚断开或处于恢复模式）："
            "请重新插拔数据线、退出恢复模式后重试。"
        )
    if isinstance(exc, IncorrectModeError):
        return IosMcpError("iPhone 当前处于恢复模式，无法执行该操作。请先退出恢复模式。")
    if isinstance(exc, (DeveloperDiskImageServiceError, InvalidServiceError, StartServiceError)):
        return IosMcpError(f"iPhone 拒绝启动所需的开发者服务。{DEVELOPER_HINT} 原始错误：{exc}")
    if isinstance(exc, DeviceVersionNotSupportedError):
        return IosMcpError(f"当前 iOS 版本不支持该操作：{exc}")
    if isinstance(exc, asyncio.TimeoutError):
        return IosMcpError(
            "与 iPhone 通信超时。请解锁并保持亮屏后重试；"
            "如果使用 tunneld，请确认 tunnel 仍在运行（`pymobiledevice3 tunneld`）。"
        )
    if isinstance(exc, PermissionError):
        return IosMcpError(
            f"权限不足：{exc}。用户态 tunnel 不需要 root；"
            "若使用 tunneld / 内核 tunnel，请检查其权限和运行状态。"
        )
    if isinstance(exc, PyMobileDevice3Exception):
        if type(exc).__name__ == "UserspaceTunnelUnavailableError":
            return IosMcpError(
                "当前设备不支持无 root 的用户态 tunnel（常见于 iOS 17.0–17.3）。"
                "请先运行 `sudo pymobiledevice3 tunneld`，然后设置 "
                "IOS_MCP_TUNNEL=tunneld 并重启 MCP server。"
                f"\n原始错误：{exc}"
            )
        return IosMcpError(f"与 iPhone 通信失败（{type(exc).__name__}）：{exc}")
    if isinstance(exc, OSError):
        return IosMcpError(f"系统 I/O 错误：{exc}。请检查数据线和 usbmuxd 是否正常。")
    return IosMcpError(f"操作失败：{exc}")


def _env_udid() -> str:
    """返回环境变量中配置的默认 UDID（可能为空）。"""
    return os.getenv("IOS_MCP_UDID", "").strip()


async def _usb_devices() -> list[MuxDevice]:
    """列出当前 usbmuxd 中的设备，USB 连接优先。"""
    try:
        devices = await list_devices()
    except Exception as exc:  # noqa: BLE001 - 统一转成人话错误
        raise friendly_error(exc) from exc
    usb = [device for device in devices if device.is_usb]
    other = [device for device in devices if not device.is_usb]
    return usb + other


async def resolve_udid(udid: str = "") -> str:
    """确定要操作的目标设备 UDID。

    :param udid: 显式指定的 UDID；为空时依次尝试 ``IOS_MCP_UDID``、第一条 USB 设备。
    :raises IosMcpError: 没有连接设备时。
    """
    wanted = (udid or "").strip() or _env_udid()
    devices = await _usb_devices()
    if not devices:
        raise IosMcpError(
            "没有检测到 iPhone。请用 USB 数据线连接 iPhone，解锁屏幕，"
            "并在手机上点“信任此电脑”。"
        )
    if wanted:
        normalised = wanted.replace("-", "").lower()
        for device in devices:
            if device.serial.replace("-", "").lower() == normalised:
                return device.serial
        raise IosMcpError(
            f"找不到 UDID 为 {wanted} 的设备。当前连接的设备："
            + "、".join(device.serial for device in devices)
        )
    return devices[0].serial


async def _device_lockdown_info(serial: str) -> dict[str, str]:
    """通过 lockdown 查询设备名称、iOS 版本等基础信息。"""
    lockdown = await asyncio.wait_for(
        create_using_usbmux(serial=serial), timeout=LOCKDOWN_TIMEOUT
    )
    try:
        values: dict[str, Any] = await asyncio.wait_for(
            lockdown.get_value(), timeout=LOCKDOWN_TIMEOUT
        )
    finally:
        with contextlib.suppress(Exception):
            await lockdown.close()

    return {
        "name": str(values.get("DeviceName") or ""),
        "ios_version": str(values.get("ProductVersion") or ""),
    }


async def list_ios_devices() -> list[dict[str, Any]]:
    """列出当前连接的 iOS 设备。

    :returns: 列表，每项包含 ``udid`` / ``name`` / ``ios_version`` / ``connected``。
    """
    devices = await _usb_devices()
    results: list[dict[str, Any]] = []
    for device in devices:
        name = ""
        ios_version = ""
        try:
            info = await _device_lockdown_info(device.serial)
            name = info["name"]
            ios_version = info["ios_version"]
        except Exception as exc:  # noqa: BLE001 - 单台设备失败不影响整体列表
            logger.warning("读取设备 %s 信息失败: %s", device.serial, friendly_error(exc))
            name = "iPhone（未解锁或未信任）"
        results.append(
            {
                "udid": device.serial,
                "name": name or "iPhone",
                "ios_version": ios_version,
                "connected": True,
            }
        )
    return results


@dataclass
class _TunnelState:
    """缓存的 tunnel / RSD 句柄。"""

    udid: str
    rsd: RemoteServiceDiscoveryService
    tunnel: Optional[Any] = None
    kind: str = "userspace"


def _rsd_is_alive(rsd: RemoteServiceDiscoveryService) -> bool:
    """粗粒度判断 RSD 的底层连接是否仍然可用。"""
    writer = getattr(getattr(rsd, "service", None), "_writer", None)
    return writer is not None and not writer.is_closing()


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    """解析 ``host:port``（支持 IPv6 的 ``[::1]:port``）。"""
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port or default_port)
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host or "127.0.0.1", int(port or default_port)
    return value or "127.0.0.1", default_port


class TunnelManager:
    """维护「每个进程最多一个」的 iOS 17+ RSD tunnel。

    pymobiledevice3 的用户态 tunnel 是进程级单例，因此同一时间只能为
    一台设备保留 tunnel；切换 UDID 时会先关闭旧的再打开新的。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: Optional[_TunnelState] = None

    async def get_rsd(self, udid: str = "") -> RemoteServiceDiscoveryService:
        """取得目标设备已连接的 RSD，必要时建立 tunnel。

        :param udid: 目标 UDID，为空时自动选择第一台 USB 设备。
        :raises IosMcpError: tunnel 建立失败时给出可操作提示。
        """
        target = await resolve_udid(udid)
        async with self._lock:
            state = self._state
            if state is not None and state.udid == target and _rsd_is_alive(state.rsd):
                return state.rsd
            await self._close_locked()
            state = await self._open_locked(target)
            self._state = state
            return state.rsd

    async def _open_locked(self, target: str) -> _TunnelState:
        """在持锁状态下建立 tunnel。"""
        rsd_env = os.getenv("IOS_MCP_RSD", "").strip()
        if rsd_env:
            host, port = _split_host_port(rsd_env, 49152)
            logger.info("连接已有 RSD %s:%s（设备 %s）", host, port, target)
            rsd = RemoteServiceDiscoveryService((host, port))
            try:
                await asyncio.wait_for(rsd.connect(), timeout=TUNNEL_OPEN_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    await rsd.close()
                raise friendly_error(exc) from exc
            return _TunnelState(udid=target, rsd=rsd, kind="rsd")

        tunnel_mode = os.getenv("IOS_MCP_TUNNEL", "").strip().lower()
        if tunnel_mode == "tunneld" or os.getenv("IOS_MCP_TUNNELD"):
            address = _split_host_port(
                os.getenv("IOS_MCP_TUNNELD", ""),
                DEFAULT_TUNNELD_ADDRESS[1],
            )
            return await self._open_tunneld(target, address)

        return await self._open_userspace(target)

    async def _open_tunneld(
        self, target: str, address: tuple[str, int]
    ) -> _TunnelState:
        """通过已运行的 tunneld 取得 RSD。"""
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid
        from pymobiledevice3.exceptions import TunneldConnectionError

        try:
            rsd = await get_tunneld_device_by_udid(target, tunneld_address=address)
        except TunneldConnectionError as exc:
            raise IosMcpError(
                f"连接不到 tunneld {address[0]}:{address[1]}。"
                "请先运行 `sudo pymobiledevice3 tunneld`，或使用默认的 userspace tunnel。"
            ) from exc
        if rsd is None:
            raise IosMcpError(
                f"tunneld（{address[0]}:{address[1]}）中没有设备 {target} 的 tunnel。"
                "请确认 iPhone 已连接、解锁并已信任此电脑。"
            )
        logger.info("已通过 tunneld 连接设备 %s", target)
        return _TunnelState(udid=target, rsd=rsd, kind="tunneld")

    async def _open_userspace(self, target: str) -> _TunnelState:
        """用 pymobiledevice3 的用户态 tunnel 建立 RSD（无需 root）。"""
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

        logger.info("为设备 %s 建立用户态 RSD tunnel…", target)
        tunnel = UserspaceRsdTunnel(serial=target)
        try:
            rsd = await asyncio.wait_for(tunnel.aopen(), timeout=TUNNEL_OPEN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await tunnel.aclose()
            raise friendly_error(exc) from exc

        logger.info("用户态 tunnel 已建立: %s", target)
        return _TunnelState(udid=target, rsd=rsd, tunnel=tunnel, kind="userspace")

    async def close(self) -> None:
        """关闭并清理缓存的 tunnel（可重复调用）。"""
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        """在持锁状态下关闭 tunnel。"""
        state, self._state = self._state, None
        if state is None:
            return
        logger.info("关闭设备 %s 的 tunnel", state.udid)
        try:
            if state.tunnel is not None:
                await asyncio.wait_for(state.tunnel.aclose(), timeout=15)
            else:
                await asyncio.wait_for(state.rsd.close(), timeout=15)
        except Exception:  # noqa: BLE001 - 清理失败不应影响后续操作
            logger.debug("关闭 tunnel 失败", exc_info=True)


#: 全进程共享的 tunnel 管理器。
manager = TunnelManager()


async def get_rsd(udid: str = "") -> RemoteServiceDiscoveryService:
    """便捷函数：取得目标设备的 RSD。"""
    return await manager.get_rsd(udid)
