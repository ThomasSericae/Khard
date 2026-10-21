"""屏幕空间的 UI 面板与按钮。

布局约定：
- 左侧：事件弹出框（点开行动槽时滑入；局面等待输入时自动弹出）
- 右上：卡牌详细说明（含性质小图标）
- 右下：性质的进一步说明（悬停性质图标时显示）
"""
from __future__ import annotations

import pygame

try:
    from . import config as C
except ImportError:
    import config as C


class Button:
    def __init__(self, rect: pygame.Rect, text: str, action: str, enabled: bool = True):
        self.rect = rect
        self.text = text
        self.action = action
        self.enabled = enabled
        self.hover = False

    def draw(self, surf: pygame.Surface):
        bg = (36, 46, 70) if self.enabled else (24, 28, 40)
        if self.hover and self.enabled:
            bg = (52, 64, 94)
        pygame.draw.rect(surf, bg, self.rect, border_radius=6)
        edge = C.GOLD if self.enabled else (70, 74, 88)
        pygame.draw.rect(surf, edge, self.rect, width=1, border_radius=6)
        img = C.font(15, bold=self.enabled).render(
            self.text, True, C.WHITE if self.enabled else C.DIM)
        surf.blit(img, img.get_rect(center=self.rect.center))


class _SlidePanel:
    """带滑入动画的左侧面板基座。子类实现 _draw_body()。"""

    WIDTH = 380
    MARGIN = 14

    def __init__(self):
        self.visible = False
        self.anim = 0.0  # 0 = 完全收起，1 = 完全展开

    def open(self):
        self.visible = True

    def close(self):
        self.visible = False

    def update(self, dt: float):
        target = 1.0 if self.visible else 0.0
        k = min(1.0, 10.0 * dt)
        self.anim += (target - self.anim) * k

    @property
    def shown(self) -> bool:
        return self.anim > 0.02

    def rect(self, screen_h: int) -> pygame.Rect:
        x = int(-self.WIDTH + (self.WIDTH + self.MARGIN) * self.anim)
        return pygame.Rect(x, self.MARGIN + 56, self.WIDTH, screen_h - self.MARGIN * 2 - 120)

    def contains(self, pos) -> bool:
        return self.shown and self._last_rect.collidepoint(pos)

    def begin_draw(self, surf: pygame.Surface, title: str) -> tuple[pygame.Rect, int]:
        """画框与标题，返回 (面板 rect, 正文起始 y)。子类接着往下画。"""
        r = self.rect(surf.get_height())
        self._last_rect = r
        panel = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
        pygame.draw.rect(panel, (*C.PANEL, 238), panel.get_rect(), border_radius=10)
        pygame.draw.rect(panel, C.PANEL_EDGE, panel.get_rect(), width=2, border_radius=10)
        surf.blit(panel, r.topleft)
        img = C.font(19, bold=True).render(title, True, C.GOLD)
        surf.blit(img, (r.x + 16, r.y + 12))
        pygame.draw.line(surf, C.PANEL_EDGE, (r.x + 12, r.y + 44),
                         (r.right - 12, r.y + 44))
        return r, r.y + 56


class EventPanel(_SlidePanel):
    """左侧事件弹出框：配方文本、子槽位、提交/放弃/开始按钮。"""

    def __init__(self, scene):
        super().__init__()
        self.scene = scene
        self.verb_uid: str | None = None
        self.buttons: list[Button] = []
        self.slot_rects: dict[str, pygame.Rect] = {}  # 供拖拽落点命中
        self._last_rect = pygame.Rect(0, 0, 0, 0)

    def open_for(self, verb_uid: str):
        self.verb_uid = verb_uid
        self.open()

    # ---- 交互 ----

    def slot_at(self, pos) -> str | None:
        for sid, r in self.slot_rects.items():
            if r.collidepoint(pos):
                return sid
        return None

    def handle(self, event) -> bool:
        """消费落在面板内的事件，返回 True 表示已消费。"""
        if not self.shown:
            return False
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for b in self.buttons:
                if b.enabled and b.rect.collidepoint(event.pos):
                    self.scene.on_panel_action(b.action)
                    return True
            if self._last_rect.collidepoint(event.pos):
                return True
        if event.type == pygame.MOUSEMOTION:
            for b in self.buttons:
                b.hover = b.rect.collidepoint(event.pos)
        return False

    # ---- 绘制 ----

    def draw(self, surf: pygame.Surface):
        if not self.shown:
            return
        self.buttons = []
        self.slot_rects = {}
        snap = self.scene.snapshot
        verb = next((v for v in snap["verbs"] if v["uid"] == self.verb_uid), None)
        if verb is None:
            self.close()
            return
        sit = next((s for s in snap["situations"] if s["uid"] == verb["situation"]), None)
        title = sit["label"] if sit else verb["label"]
        r, y = self.begin_draw(surf, title)
        left, width = r.x + 16, r.width - 32

        if sit:
            y = self._draw_situation(surf, r, y, left, width, sit)
        else:
            y = self._draw_idle(surf, r, y, left, width, verb)

    def _draw_situation(self, surf, r, y, left, width, sit) -> int:
        # 正文
        text = sit["start_text"] or "……"
        for line in C.wrap_text(text, C.font(14), width):
            surf.blit(C.font(14).render(line, True, C.WHITE), (left, y))
            y += 20
        y += 8
        if sit["phase"] == "running" and sit["warmup"] > 0:
            hint = f"进行中……还需 {sit['warmup'] - sit['elapsed']} 回合"
            surf.blit(C.font(13).render(hint, True, C.DIM), (left, y))
            y += 24
        elif sit["phase"] == "awaiting_input":
            surf.blit(C.font(13, bold=True).render("等待你的决断", True, C.GOLD), (left, y))
            y += 24

        # 主卡口
        y = self._draw_card_row(surf, left, y, "主卡口", sit["main"])
        # 子槽位
        for s in sit["slots"]:
            rect = pygame.Rect(left, y, width, 44)
            self.slot_rects[s["id"]] = rect
            empty = not s["cards"]
            bg = (30, 38, 58) if not (s["essential"] and empty) else (52, 40, 34)
            pygame.draw.rect(surf, bg, rect, border_radius=6)
            edge = C.GOLD if s["essential"] and empty else (70, 80, 105)
            pygame.draw.rect(surf, edge, rect, width=1, border_radius=6)
            req = "、".join(f"{self.scene.aspect_label(k)}×{v}" for k, v in s["required"].items())
            head = f"{s['label']}" + (f"（需：{req}）" if req else "")
            if s["greedy"]:
                head += " [自动]"
            if s["essential"]:
                head += " *"
            surf.blit(C.font(12).render(head, True, C.DIM), (rect.x + 8, rect.y + 4))
            names = "、".join(self.scene.card_label(uid) for uid in s["cards"]) or "（空）"
            surf.blit(C.font(14).render(names, True, C.WHITE), (rect.x + 8, rect.y + 20))
            y += 52

        # 按钮
        if sit["phase"] == "awaiting_input":
            by = r.bottom - 52
            self.buttons = [
                Button(pygame.Rect(left, by, width // 2 - 6, 36), "提交", "commit"),
                Button(pygame.Rect(left + width // 2 + 6, by, width // 2 - 6, 36), "放弃", "abort"),
            ]
        else:
            self.buttons = [Button(pygame.Rect(left, r.bottom - 52, width, 36), "合上", "close")]
        for b in self.buttons:
            b.draw(surf)
        return y

    def _draw_idle(self, surf, r, y, left, width, verb) -> int:
        cards = self.scene.cards_in_verb(verb["uid"])
        y = self._draw_card_row(surf, left, y, "主卡口", cards)
        previews = self.scene.preview_verb(verb["uid"])
        if previews:
            surf.blit(C.font(13).render("可能发生：", True, C.DIM), (left, y))
            y += 20
            for p in previews[:3]:
                for line in C.wrap_text(f"· {p['label']}：{p['start_text']}", C.font(13), width)[:2]:
                    surf.blit(C.font(13).render(line, True, C.WHITE), (left, y))
                    y += 18
        else:
            surf.blit(C.font(13).render("把卡拖进来，或直接开始默认事件。", True, C.DIM),
                      (left, y))
            y += 20
        self.buttons = [
            Button(pygame.Rect(left, r.bottom - 52, width // 2 - 6, 36),
                   "开始", "begin", enabled=bool(previews)),
            Button(pygame.Rect(left + width // 2 + 6, r.bottom - 52, width // 2 - 6, 36),
                   "合上", "close"),
        ]
        for b in self.buttons:
            b.draw(surf)
        return y

    def _draw_card_row(self, surf, left, y, title, uids) -> int:
        surf.blit(C.font(12).render(title, True, C.DIM), (left, y))
        y += 18
        names = "、".join(self.scene.card_label(uid) for uid in uids) or "（空）"
        for line in C.wrap_text(names, C.font(14), 340):
            surf.blit(C.font(14).render(line, True, C.WHITE), (left, y))
            y += 20
        return y + 6


class DetailPanel:
    """右上角：悬停/选中卡牌的详细说明，含性质小图标。"""

    WIDTH = 300

    def __init__(self, scene):
        self.scene = scene
        self.chip_rects: list[tuple[pygame.Rect, str]] = []  # (rect, 性质id)
        self.rect: pygame.Rect | None = None                 # 供悬停粘性判定

    def aspect_at(self, pos) -> str | None:
        for r, aid in self.chip_rects:
            if r.collidepoint(pos):
                return aid
        return None

    def contains(self, pos) -> bool:
        return self.rect is not None and self.rect.collidepoint(pos)

    def draw(self, surf: pygame.Surface, card: dict | None):
        self.chip_rects = []
        self.rect = None
        if card is None:
            return
        w, m = self.WIDTH, 14
        x0 = surf.get_width() - w - m
        d = self.scene.content.cards[card["def"]]

        # 估算高度：标题 + 描述 + 图标行
        desc_lines = C.wrap_text(d.description or "（暂无描述）", C.font(13), w - 32)
        h = 56 + len(desc_lines) * 18 + (44 if d.aspects else 0) + 16
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(panel, (*C.PANEL, 238), panel.get_rect(), border_radius=10)
        pygame.draw.rect(panel, C.PANEL_EDGE, panel.get_rect(), width=2, border_radius=10)
        surf.blit(panel, (x0, m))
        self.rect = pygame.Rect(x0, m, w, h)

        y = m + 10
        title = card["label"] + (f" ×{card['stack']}" if card["stack"] > 1 else "")
        surf.blit(C.font(17, bold=True).render(title, True, C.WHITE), (x0 + 16, y))
        if card["life"] is not None:
            life = C.font(12).render(f"余 {card['life']} 回合", True, C.DIM)
            surf.blit(life, (x0 + w - 16 - life.get_width(), y + 4))
        y += 26
        for line in desc_lines:
            surf.blit(C.font(13).render(line, True, C.DIM), (x0 + 16, y))
            y += 18
        y += 8

        # 性质小图标：色块 + 首字 + 数量
        for i, (aid, qty) in enumerate(d.aspects.items()):
            cx = x0 + 16 + i * 46
            rect = pygame.Rect(cx, y, 38, 38)
            self.chip_rects.append((rect, aid))
            color = self.scene.aspect_colors.get(aid, C.GOLD)
            pygame.draw.rect(surf, color, rect, border_radius=6)
            pygame.draw.rect(surf, C.INK, rect, width=1, border_radius=6)
            ch = C.font(15, bold=True).render(self.scene.aspect_label(aid)[0], True, C.INK)
            surf.blit(ch, ch.get_rect(center=(rect.centerx, rect.centery - 5)))
            n = C.font(11, bold=True).render(str(qty), True, C.WHITE)
            surf.blit(n, n.get_rect(center=(rect.centerx, rect.bottom - 8)))


class AspectPanel:
    """右下角：性质的进一步说明。"""

    WIDTH = 300

    def __init__(self, scene):
        self.scene = scene

    def draw(self, surf: pygame.Surface, aspect_id: str | None):
        if aspect_id is None or aspect_id not in self.scene.content.aspects:
            return
        a = self.scene.content.aspects[aspect_id]
        w, m = self.WIDTH, 14
        x0 = surf.get_width() - w - m
        lines = C.wrap_text(a.description or "（暂无描述）", C.font(13), w - 32)
        h = 44 + len(lines) * 18 + 12
        y0 = surf.get_height() - h - m
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(panel, (*C.PANEL, 238), panel.get_rect(), border_radius=10)
        color = self.scene.aspect_colors.get(aspect_id, C.GOLD)
        pygame.draw.rect(panel, color, panel.get_rect(), width=2, border_radius=10)
        surf.blit(panel, (x0, y0))
        surf.blit(C.font(15, bold=True).render(a.label, True, color), (x0 + 16, y0 + 10))
        y = y0 + 32
        for line in lines:
            surf.blit(C.font(13).render(line, True, C.WHITE), (x0 + 16, y))
            y += 18


def aspect_chip(surf, pos, color, char, qty=1, size=38):
    """画一个性质小图标：色块 + 首字 + 数量。详情面板与事件面板共用。"""
    rect = pygame.Rect(pos[0], pos[1], size, size)
    pygame.draw.rect(surf, color, rect, border_radius=6)
    pygame.draw.rect(surf, C.INK, rect, width=1, border_radius=6)
    ch = C.font(size * 0.4, bold=True).render(char, True, C.INK)
    surf.blit(ch, ch.get_rect(center=(rect.centerx, rect.centery - size * 0.12)))
    if qty > 1:
        n = C.font(size * 0.28, bold=True).render(str(qty), True, C.WHITE)
        surf.blit(n, n.get_rect(center=(rect.centerx, rect.bottom - size * 0.2)))
    return rect