"""桌面上的视图对象：卡牌视图与行动槽视图。

视图持有「视觉位置」，与引擎的逻辑位置分离：
每次 sync() 从快照更新目标位置，update() 里做指数平滑，
于是拖拽跟手、引擎挪卡（如事件结束退回桌面）都有滑动动画。
"""
from __future__ import annotations

import math
import random

import pygame

try:
    from . import config as C
except ImportError:
    import config as C

_glow_cache: dict[tuple[int, int, int], pygame.Surface] = {}


def _glow(size: int, alpha: int) -> pygame.Surface:
    """圆角光晕 surface，按 (尺寸, 量化透明度) 缓存。"""
    q = alpha // 16 * 16
    key = (size, q, 0)
    if key not in _glow_cache:
        pad = 10
        s = pygame.Surface((size + pad * 2, size + pad * 2), pygame.SRCALPHA)
        for i, a in ((8, q // 3), (4, q // 2), (2, q)):
            pygame.draw.rect(s, (*C.GOLD, min(255, a)),
                             (pad - i, pad - i, size + i * 2, size + i * 2),
                             width=3, border_radius=10 + i)
        _glow_cache[key] = s
    return _glow_cache[key]


def cell_center(x: int, y: int) -> tuple[float, float]:
    """格子 (x, y) 的世界中心。"""
    return ((x + 0.5) * C.CELL, (y + 0.5) * C.CELL)


class CardView:
    """一张卡的视图。data 是快照里该卡的条目。"""

    def __init__(self, uid: str, data: dict, pos: tuple[float, float]):
        self.uid = uid
        self.x, self.y = pos
        self.tx, self.ty = pos
        self.data = data
        self.phase = random.random() * math.tau  # 光晕相位错开，避免全场同步闪烁
        self.dragging = False
        self.hover = False
        self.visible = True  # 被关押进局面的卡不在桌面绘制

    def sync(self, data: dict, target: tuple[float, float] | None):
        """用快照条目更新自己。target 为 None 表示不在桌面显示（在局面/面板里）。"""
        self.data = data
        self.visible = target is not None
        if target is not None:
            self.tx, self.ty = target

    def update(self, dt: float):
        if not self.dragging:
            k = min(1.0, C.LERP * dt)
            self.x += (self.tx - self.x) * k
            self.y += (self.ty - self.y) * k

    def screen_rect(self, cam) -> pygame.Rect:
        s = C.CARD * cam.zoom
        sx, sy = cam.world_to_screen(self.x, self.y)
        return pygame.Rect(int(sx - s / 2), int(sy - s / 2), int(s), int(s))

    def draw(self, surf: pygame.Surface, cam, t: float, aspect_colors: dict[str, tuple]):
        r = self.screen_rect(cam)
        if r.width < 6:  # 缩得太小就不费事画了
            return
        d = self.data

        # 光晕：悬停/拖拽时脉动
        if self.hover or self.dragging:
            alpha = int(110 + 70 * math.sin(t * 4 + self.phase))
            g = _glow(r.width, alpha)
            surf.blit(g, (r.x - 10, r.y - 10))

        # 卡体 + 顶部性质色带
        pygame.draw.rect(surf, C.CARD_BODY, r, border_radius=max(2, int(6 * cam.zoom)))
        if d["aspects"]:
            first = next(iter(d["aspects"]))
            band = pygame.Rect(r.x, r.y, r.width, max(2, int(6 * cam.zoom)))
            pygame.draw.rect(surf, aspect_colors.get(first, C.GOLD), band,
                             border_top_left_radius=band.height, border_top_right_radius=band.height)
        edge = C.GOLD if (self.hover or self.dragging) else (70, 80, 105)
        pygame.draw.rect(surf, edge, r, width=max(1, int(2 * cam.zoom)),
                         border_radius=max(2, int(6 * cam.zoom)))

        # 名称
        f = C.font(13 * cam.zoom, bold=True)
        label = d["label"]
        while len(label) > 1 and f.size(label)[0] > r.width - 6:
            label = label[:-1]
        img = f.render(label, True, C.WHITE)
        surf.blit(img, img.get_rect(center=(r.centerx, r.centery + r.height // 8)))

        # 堆叠数徽章
        if d["stack"] > 1:
            rad = max(6, int(9 * cam.zoom))
            pos = (r.right - rad, r.y + rad)
            pygame.draw.circle(surf, C.GOLD, pos, rad)
            pygame.draw.circle(surf, C.INK, pos, rad, width=1)
            n = C.font(11 * cam.zoom, bold=True).render(str(d["stack"]), True, C.INK)
            surf.blit(n, n.get_rect(center=pos))

        # 剩余寿命
        if d["life"] is not None:
            n = C.font(10 * cam.zoom).render(str(d["life"]), True, C.DIM)
            surf.blit(n, (r.x + 3, r.bottom - n.get_height() - 2))


class VerbView:
    """行动槽视图。运行事件时从槽底向槽顶逐渐亮起（高度 = elapsed/warmup）。

    与 CardView 一样持有视觉位置：拖动跟手，引擎挪位（如吸附网格）平滑滑过去。
    """

    def __init__(self, uid: str, data: dict):
        self.uid = uid
        self.data = data
        self.x = data["x"] * C.CELL   # 视觉位置（世界坐标，左上角）
        self.y = data["y"] * C.CELL
        self.tx, self.ty = self.x, self.y
        self.dragging = False
        self.hover = False

    def sync(self, data: dict):
        self.data = data
        self.tx = data["x"] * C.CELL
        self.ty = data["y"] * C.CELL

    def update(self, dt: float):
        if not self.dragging:
            k = min(1.0, C.LERP * dt)
            self.x += (self.tx - self.x) * k
            self.y += (self.ty - self.y) * k

    def screen_rect(self, cam) -> pygame.Rect:
        d = self.data
        x0, y0 = cam.world_to_screen(self.x, self.y)
        return pygame.Rect(int(x0), int(y0),
                           int(d["w"] * C.CELL * cam.zoom),
                           int(d["h"] * C.CELL * cam.zoom))

    def draw(self, surf: pygame.Surface, cam, t: float,
             situation: dict | None):
        r = self.screen_rect(cam)
        if r.width < 8:
            return
        d = self.data
        pygame.draw.rect(surf, C.VERB_BODY, r, border_radius=max(2, int(8 * cam.zoom)))

        running = situation is not None and situation["phase"] == "running"
        waiting = situation is not None and situation["phase"] == "awaiting_input"

        # 底部向上的进度光带
        if running and situation["warmup"] > 0:
            ratio = min(1.0, situation["elapsed"] / situation["warmup"])
            fill_h = int(r.height * ratio)
            if fill_h > 0:
                grad = pygame.Surface((r.width, fill_h), pygame.SRCALPHA)
                for i in range(fill_h):  # 越靠槽底越亮
                    a = int(150 * (1 - i / fill_h) ** 1.5) + 20
                    pygame.draw.line(grad, (*C.GOLD, a), (0, fill_h - 1 - i),
                                     (r.width, fill_h - 1 - i))
                surf.blit(grad, (r.x, r.bottom - fill_h))

        # 边框：等待输入时金色闪烁，运行中常亮金，空闲暗色
        if waiting:
            a = int(140 + 100 * math.sin(t * 6))
            edge = (*C.GOLD,)
            w_surf = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
            pygame.draw.rect(w_surf, (*edge, a), w_surf.get_rect(),
                             width=max(2, int(3 * cam.zoom)),
                             border_radius=max(2, int(8 * cam.zoom)))
            surf.blit(w_surf, r.topleft)
        else:
            edge = C.GOLD if running or self.hover else (70, 80, 105)
            pygame.draw.rect(surf, edge, r, width=max(1, int(2 * cam.zoom)),
                             border_radius=max(2, int(8 * cam.zoom)))

        # 槽名 + 状态行
        f = C.font(14 * cam.zoom, bold=True)
        img = f.render(d["label"], True, C.WHITE if running else C.DIM)
        surf.blit(img, img.get_rect(midtop=(r.centerx, r.y + int(6 * cam.zoom))))
        if situation is not None:
            sf = C.font(11 * cam.zoom)
            sub = situation["label"]
            if situation["warmup"] > 0:
                sub += f"  {situation['elapsed']}/{situation['warmup']} 回合"
            s_img = sf.render(sub, True, C.GOLD)
            surf.blit(s_img, s_img.get_rect(midbottom=(r.centerx, r.bottom - int(6 * cam.zoom))))