"""相机：世界坐标 ↔ 屏幕坐标，支持以光标为锚的缩放与平移。"""
from __future__ import annotations

try:  # 作为包导入（python main.py）
    from . import config as C
except ImportError:  # frontend 目录直接在 sys.path 上（IDE sources root 模式）
    import config as C


class Camera:
    def __init__(self, screen_size: tuple[int, int]):
        self.x = 0.0    # 视野中心的世界坐标
        self.y = 0.0
        self.zoom = 1.0
        self.screen_w, self.screen_h = screen_size

    def resize(self, w: int, h: int):
        self.screen_w, self.screen_h = w, h

    def world_to_screen(self, wx: float, wy: float) -> tuple[float, float]:
        return ((wx - self.x) * self.zoom + self.screen_w / 2,
                (wy - self.y) * self.zoom + self.screen_h / 2)

    def screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self.screen_w / 2) / self.zoom + self.x,
                (sy - self.screen_h / 2) / self.zoom + self.y)

    def zoom_at(self, sx: float, sy: float, factor: float):
        """以屏幕点 (sx, sy) 为锚缩放：锚点下的世界位置保持不动。"""
        wx, wy = self.screen_to_world(sx, sy)
        self.zoom = max(C.ZOOM_MIN, min(C.ZOOM_MAX, self.zoom * factor))
        self.x = wx - (sx - self.screen_w / 2) / self.zoom
        self.y = wy - (sy - self.screen_h / 2) / self.zoom

    def pan_pixels(self, dx: float, dy: float):
        """按屏幕像素增量平移（桌面跟随光标）。"""
        self.x -= dx / self.zoom
        self.y -= dy / self.zoom