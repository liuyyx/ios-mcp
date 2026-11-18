"""屏幕内容理解：OCR 文本定位、模板匹配与像素/逻辑坐标换算。

截图来自 ``ios_mcp.screen.capture_screenshot``，是物理像素（例如
1125x2436）；``ios_tap`` 等工具接收逻辑点（例如 375x812）。本模块负责
两者之间的换算，并给出可直接点击的坐标。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .device import IosMcpError
from .screen import capture_screenshot, screen_size

logger = logging.getLogger(__name__)

#: OCR 引擎是进程级单例，模型加载较慢，且不是线程安全的。
_ocr_engine: Any = None
_ocr_lock = threading.Lock()

#: 模板匹配默认阈值（归一化相关系数）。
DEFAULT_MATCH_THRESHOLD = 0.8


def _get_ocr_engine() -> Any:
    """惰性加载 RapidOCR（PP-OCR 的 onnxruntime 版本，离线、跨平台）。"""
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except Exception as exc:  # noqa: BLE001
                    raise IosMcpError(
                        "OCR 需要 rapidocr-onnxruntime。请运行："
                        "pip install rapidocr-onnxruntime"
                    ) from exc
                _ocr_engine = RapidOCR()
    return _ocr_engine


def decode_image(data: bytes) -> np.ndarray:
    """把 PNG/JPEG 字节解码成 BGR ndarray。"""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise IosMcpError("无法解码截图数据（不是有效的 PNG/JPEG）。")
    return image


def decode_base64_image(value: str) -> np.ndarray:
    """解码模板图片；允许带 ``data:image/png;base64,`` 前缀。"""
    text = value.strip()
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise IosMcpError(f"模板图不是合法的 base64：{exc}") from exc
    return decode_image(raw)


@dataclass(frozen=True)
class TextHit:
    """一段 OCR 文本及其位置（物理像素）。"""

    text: str
    confidence: float
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def to_payload(
        self, pixel_size: tuple[int, int], point_size: tuple[int, int]
    ) -> dict[str, Any]:
        """转成 MCP 返回用的字典，同时给出像素框和逻辑点。"""
        cx, cy = self.center
        px, py = scale_to_points(cx, cy, pixel_size, point_size)
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "pixel_box": {
                "x": self.left,
                "y": self.top,
                "width": self.width,
                "height": self.height,
            },
            "point": {"x": px, "y": py},
        }


def scale_to_points(
    x: int, y: int, pixel_size: tuple[int, int], point_size: tuple[int, int]
) -> tuple[int, int]:
    """物理像素坐标 -> 逻辑点坐标（``ios_tap`` / ``ios_swipe`` 使用）。"""
    pw, ph = pixel_size
    lw, lh = point_size
    if pw <= 0 or ph <= 0 or lw <= 0 or lh <= 0:
        raise IosMcpError(f"无效的尺寸：像素 {pixel_size}，逻辑点 {point_size}")
    return round(x * lw / pw), round(y * lh / ph)


def _ocr_sync(image: np.ndarray) -> list[TextHit]:
    engine = _get_ocr_engine()
    with _ocr_lock:
        result, _ = engine(image)
    hits: list[TextHit] = []
    for item in result or []:
        box, text, score = item[0], item[1], item[2]
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        hits.append(
            TextHit(
                text=str(text),
                confidence=float(score),
                left=int(min(xs)),
                top=int(min(ys)),
                right=int(max(xs)),
                bottom=int(max(ys)),
            )
        )
    return hits


async def ocr_bytes(png: bytes) -> tuple[list[TextHit], tuple[int, int]]:
    """对已有 PNG 截图做 OCR，避免描述屏幕时重复截图。"""
    image = decode_image(png)
    height, width = image.shape[:2]
    hits = await asyncio.to_thread(_ocr_sync, image)
    return hits, (width, height)


async def ocr_hits(udid: str = "") -> tuple[list[TextHit], tuple[int, int], tuple[int, int]]:
    """截屏并做 OCR。

    :returns: ``(命中列表, 像素尺寸, 逻辑点尺寸)``
    """
    png = await capture_screenshot(udid)
    hits, pixel_size = await ocr_bytes(png)
    logical = await screen_size(udid)
    return hits, pixel_size, logical


def match_template_hits(
    screen: np.ndarray,
    template: np.ndarray,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    max_results: int = 10,
) -> list[tuple[int, int, int, int, float]]:
    """在截图里找模板，返回 ``(left, top, width, height, score)`` 列表。

    使用归一化相关系数；模板必须不大于截图，否则直接报错。
    """
    sh, sw = screen.shape[:2]
    th, tw = template.shape[:2]
    if th > sh or tw > sw:
        raise IosMcpError(
            f"模板尺寸 {tw}x{th} 大于截图 {sw}x{sh}。请换一张更小的模板图。"
        )
    if th < 4 or tw < 4:
        raise IosMcpError(f"模板太小（{tw}x{th}），至少需要 4x4 像素。")
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    matches: list[tuple[int, int, int, int, float]] = []
    work = result.copy()
    for _ in range(max(1, max_results)):
        _, score, _, location = cv2.minMaxLoc(work)
        if score < threshold:
            break
        x, y = int(location[0]), int(location[1])
        matches.append((x, y, tw, th, float(score)))
        # 抑制邻域，避免同一个图标被重复命中。
        x0 = max(0, x - tw // 2)
        y0 = max(0, y - th // 2)
        x1 = min(work.shape[1], x + tw // 2 + 1)
        y1 = min(work.shape[0], y + th // 2 + 1)
        work[y0:y1, x0:x1] = -1.0
    return matches


async def ocr_screen_payload(udid: str = "") -> dict[str, Any]:
    """返回 OCR 结果（含可点击的逻辑点坐标）。"""
    hits, pixel_size, point_size = await ocr_hits(udid)
    items = [hit.to_payload(pixel_size, point_size) for hit in hits]
    return {
        "pixel_size": {"width": pixel_size[0], "height": pixel_size[1]},
        "point_size": {"width": point_size[0], "height": point_size[1]},
        "count": len(items),
        "texts": items,
    }


async def find_text_payload(
    text: str, udid: str = "", exact: bool = False
) -> dict[str, Any]:
    """在屏幕上按文本查找，返回匹配项与可点击坐标。"""
    needle = text.strip()
    if not needle:
        raise IosMcpError("text 不能为空。")
    hits, pixel_size, point_size = await ocr_hits(udid)
    lowercase = needle.lower()
    matched = [
        hit
        for hit in hits
        if (hit.text == needle if exact else lowercase in hit.text.lower())
    ]
    items = [hit.to_payload(pixel_size, point_size) for hit in matched]
    return {
        "query": needle,
        "exact": exact,
        "pixel_size": {"width": pixel_size[0], "height": pixel_size[1]},
        "point_size": {"width": point_size[0], "height": point_size[1]},
        "count": len(items),
        "matches": items,
    }


async def find_image_payload(
    template_base64: str,
    udid: str = "",
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    """模板匹配，返回匹配框与可点击坐标。"""
    png = await capture_screenshot(udid)
    screen = decode_image(png)
    template = decode_base64_image(template_base64)
    height, width = screen.shape[:2]
    logical = await screen_size(udid)
    raw = match_template_hits(screen, template, threshold=threshold)
    matches = []
    for left, top, tw, th, score in raw:
        cx, cy = left + tw // 2, top + th // 2
        px, py = scale_to_points(cx, cy, (width, height), logical)
        matches.append(
            {
                "score": round(score, 4),
                "pixel_box": {"x": left, "y": top, "width": tw, "height": th},
                "point": {"x": px, "y": py},
            }
        )
    return {
        "threshold": threshold,
        "template_size": {"width": template.shape[1], "height": template.shape[0]},
        "pixel_size": {"width": width, "height": height},
        "point_size": {"width": logical[0], "height": logical[1]},
        "count": len(matches),
        "matches": matches,
    }
