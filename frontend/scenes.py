"""场景：主菜单（pygame-menu）、游戏桌面、暂停菜单、结局演出。

手势约定（触屏上 SDL 把单指映射为鼠标左键，故同一套逻辑两用）：
- 单击卡牌：在右上详情面板「打开」它——松手后保持显示，再点别处取消
- 单击行动槽：左侧滑出事件弹出框
- 按住拖动：移动卡牌 / 行动槽；拖空白处平移桌面（鼠标右键/中键拖动亦可）
- 单击详情面板的性质图标：右下显示该性质的进一步说明（再点同一图标取消）
- 双击卡牌：自动填入距离最近的行动槽里第一个能接纳它的槽位
- 滚轮 / HUD 的 ± 按钮缩放；平台派发 MULTIGESTURE 时支持双指捏合缩放
"""
from __future__ import annotations

import json
import math
import os
import time

import pygame
import pygame_menu

from engine import ContentBase, Game, GameError

try:
    from . import config as C
    from .camera import Camera
    from .panels import Button, EventPanel, DetailPanel, AspectPanel
    from .sprites import CardView, VerbView, cell_center
except ImportError:
    import config as C
    from camera import Camera
    from panels import Button, EventPanel, DetailPanel, AspectPanel
    from sprites import CardView, VerbView, cell_center

CLICK_MAX_DIST = 8     # 按下与松开的屏幕像素距离小于它视为「单击」而非拖动
DBL_CLICK_MS = 400     # 双击间隔上限（毫秒）


def _menu_theme() -> pygame_menu.Theme:
    t = pygame_menu.themes.THEME_DARK.copy()
    t.background_color = C.INK
    t.title_font_color = C.GOLD
    t.widget_font_color = C.WHITE
    t.selection_color = C.GOLD
    cjk = C._find_cjk_font()
    if cjk:
        t.title_font = cjk
        t.widget_font = cjk
    t.title_font_size = 44
    t.widget_font_size = 24
    return t


class MenuScene:
    """开始界面：新的一局 / 读取存档 / 退出。"""

    def __init__(self, app):
        self.app = app
        w, h = app.screen.get_size()
        self.menu = pygame_menu.Menu("密教引擎", w, h, theme=_menu_theme())
        self.menu.add.label("一张桌子，一些牌，和不可名状之事。", font_size=16)
        self.menu.add.vertical_margin(30)
        self.menu.add.button("新的一局", lambda: app.start_game())
        if os.path.exists(C.SAVE_PATH):
            self.menu.add.button("读取存档", lambda: app.start_game(load_save=True))
        self.menu.add.button("退出", app.quit)

    def handle(self, events) -> bool:
        self.menu.update(events)
        return True

    def update(self, dt: float):
        pass

    def draw(self, surf: pygame.Surface):
        self.menu.draw(surf)


class GameScene:
    """大桌子：卡牌与行动槽的世界视图 + 三块屏幕面板 + HUD。"""

    def __init__(self, app, content_path: str,
                 load_save: bool = False, legacy_spawn: dict | None = None):
        self.app = app
        self.content = ContentBase.load_file(content_path)
        if load_save and os.path.exists(C.SAVE_PATH):
            with open(C.SAVE_PATH, encoding="utf-8") as f:
                self.game = Game.from_dict(self.content, json.load(f))
        else:
            self.game = Game(self.content, legacy_spawn=legacy_spawn)
            # 开局注入（getattr 容错：model.py 未同步更新时也不至于崩）
            for cid, n in getattr(self.content, "start_spawn", {}).items():
                self.game.spawn_card(cid, n)

        self.cam = Camera(app.screen.get_size())
        self.cam.x, self.cam.y = 2 * C.CELL, 2 * C.CELL
        self.snapshot = self.game.snapshot()
        self.card_views: dict[str, CardView] = {}
        self.verb_views: dict[str, VerbView] = {}
        self.aspect_colors = {a: C.ASPECT_PALETTE[i % len(C.ASPECT_PALETTE)]
                              for i, a in enumerate(self.content.aspects)}

        # 手势状态机
        self._press: dict | None = None  # {"kind": card/verb/empty, "uid", "pos", "last", "from", "moved"}
        self._last_click: tuple[float, str | None] = (0.0, None)  # 双击检测
        self._panning = False              # 右键/中键平移（触屏无此键，用拖空白处替代）
        self._pan_last = (0, 0)

        # 显示状态
        self.hover_uid: str | None = None
        self._sticky_card: str | None = None    # 单击「打开」的卡牌详情
        self.pinned_aspect: str | None = None   # 单击性质图标后固定的说明
        self.toasts: list[list] = []            # [text, ttl]

        # 面板与 HUD
        self.event_panel = EventPanel(self)
        self.detail_panel = DetailPanel(self)
        self.aspect_panel = AspectPanel(self)
        self.pause_menu: pygame_menu.Menu | None = None
        self.ending_id: str | None = None
        self._ending_buttons: list[Button] = []
        sw, sh = app.screen.get_size()
        self.end_turn_btn = Button(pygame.Rect(16, sh - 56, 140, 40), "结束回合 ▸", "end_turn")
        self.zoom_out_btn = Button(pygame.Rect(168, sh - 56, 44, 40), "−", "zoom_out")
        self.zoom_in_btn = Button(pygame.Rect(220, sh - 56, 44, 40), "＋", "zoom_in")

        # 背景贴图
        self.bg = None
        if os.path.exists(C.BG_PATH):
            self.bg = pygame.image.load(C.BG_PATH).convert()

        self.sync()

    # ==================== 与引擎的同步 ====================

    def sync(self):
        """引擎每次变动后调用：对齐视图与快照。视图按 uid 复用以保住动画连续性。"""
        self.snapshot = self.game.snapshot()

        # 行动槽
        seen_v = set()
        for v in self.snapshot["verbs"]:
            seen_v.add(v["uid"])
            if v["uid"] in self.verb_views:
                self.verb_views[v["uid"]].sync(v)
            else:
                self.verb_views[v["uid"]] = VerbView(v["uid"], v)
        for uid in list(self.verb_views):
            if uid not in seen_v:
                del self.verb_views[uid]

        # 卡牌目标位置：桌面格 / 槽内卡口（跟随槽的视觉位置，拖槽时牌跟着走）/ 局面内（不画）
        seen_c = set()
        verb_card_count: dict[str, int] = {}
        for c in self.snapshot["cards"]:
            seen_c.add(c["uid"])
            loc = c["location"]
            target = None
            if loc.get("kind") == "table":
                target = cell_center(loc["x"], loc["y"])
            elif loc.get("kind") == "verb":
                vv = self.verb_views.get(loc["verb"])
                if vv is not None:
                    i = verb_card_count.get(loc["verb"], 0)
                    verb_card_count[loc["verb"]] = i + 1
                    target = (vv.x + (i % 2 + 0.5) * C.CELL,
                              vv.y + (i // 2 + 0.62) * C.CELL)
            if c["uid"] in self.card_views:
                self.card_views[c["uid"]].sync(c, target)
            else:
                pos = target or cell_center(0, 0)
                self.card_views[c["uid"]] = CardView(c["uid"], c, pos)
        for uid in list(self.card_views):
            if uid not in seen_c:
                del self.card_views[uid]

        # 若有局面在等待输入且面板未开，自动弹出
        for s in self.snapshot["situations"]:
            if s["phase"] == "awaiting_input" and self.event_panel.verb_uid != s["verb"]:
                self.event_panel.open_for(s["verb"])

    # ==================== 面板需要的查询 ====================

    def verbs_by_uid(self) -> dict[str, dict]:
        return {v["uid"]: v for v in self.snapshot["verbs"]}

    def card_label(self, uid: str) -> str:
        for c in self.snapshot["cards"]:
            if c["uid"] == uid:
                return c["label"] + (f"×{c['stack']}" if c["stack"] > 1 else "")
        return "?"

    def aspect_label(self, aid: str) -> str:
        a = self.content.aspects.get(aid)
        return a.label if a else aid

    def cards_in_verb(self, verb_uid: str) -> list[str]:
        return [c["uid"] for c in self.snapshot["cards"]
                if c["location"].get("kind") == "verb"
                and c["location"]["verb"] == verb_uid]

    def preview_verb(self, verb_uid: str) -> list[dict]:
        try:
            return self.game.preview(verb_uid)
        except GameError:
            return []

    def toast(self, msg: str):
        self.toasts.append([msg, 2.2])

    # ==================== 面板按钮动作 ====================

    def on_panel_action(self, action: str):
        verb_uid = self.event_panel.verb_uid
        try:
            if action == "begin":
                self.game.begin(verb_uid)
            elif action == "commit":
                sit = self._situation_of(verb_uid)
                if sit:
                    self.game.commit(sit["uid"])
            elif action == "abort":
                sit = self._situation_of(verb_uid)
                if sit:
                    self.game.abort(sit["uid"])
            elif action == "close":
                self.event_panel.close()
                return
        except GameError as e:
            self.toast(str(e))
        self.sync()
        self._check_ending()

    def _situation_of(self, verb_uid: str) -> dict | None:
        for s in self.snapshot["situations"]:
            if s["verb"] == verb_uid:
                return s
        return None

    def _check_ending(self):
        if self.game.over and self.ending_id is None:
            self.ending_id = self.game.over
            e = self.content.endings.get(self.ending_id)
            sw, sh = self.app.screen.get_size()
            bw, bh = 180, 44
            cx, cy = sw // 2, sh // 2 + 110
            self._ending_buttons = [
                Button(pygame.Rect(cx - bw - 14, cy, bw, bh), "带着传承再开", "legacy"),
                Button(pygame.Rect(cx + 14, cy, bw, bh), "回到主菜单", "menu"),
            ]

    # ==================== 手势状态机 ====================

    def handle(self, events) -> bool:
        if self.pause_menu is not None:
            self.pause_menu.update(events)
            for ev in events:
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    self.pause_menu = None
            return True

        for ev in events:
            if self.ending_id is not None:
                self._handle_ending(ev)
                continue
            if ev.type == pygame.MOUSEWHEEL:
                mx, my = pygame.mouse.get_pos()
                self.cam.zoom_at(mx, my, 1.12 ** ev.y)
            elif ev.type == pygame.MULTIGESTURE:
                self._on_pinch(ev)
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (2, 3):
                self._panning = True
                self._pan_last = ev.pos
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button in (2, 3):
                self._panning = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                self._on_left_down(ev)
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                self._on_left_up(ev.pos)
            elif ev.type == pygame.MOUSEMOTION:
                self.event_panel.handle(ev)
                if self._panning:
                    self.cam.pan_pixels(ev.pos[0] - self._pan_last[0],
                                        ev.pos[1] - self._pan_last[1])
                    self._pan_last = ev.pos
                else:
                    self._on_drag_motion(ev.pos)
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                self._open_pause_menu()
            elif ev.type == pygame.VIDEORESIZE:
                self.cam.resize(ev.w, ev.h)
        return True

    def _on_left_down(self, ev):
        """按下：确定手势对象（HUD > 面板 > 性质图标 > 卡牌 > 行动槽 > 空白），
        但还不判定是单击还是拖动——松开时按位移距离裁决。"""
        pos = ev.pos
        # HUD 按钮
        if self.end_turn_btn.rect.collidepoint(pos):
            self._end_turn()
            return
        if self.zoom_in_btn.rect.collidepoint(pos):
            self._zoom(1.25)
            return
        if self.zoom_out_btn.rect.collidepoint(pos):
            self._zoom(0.8)
            return
        # 左侧事件面板内部
        if self.event_panel.handle(ev):
            return
        # 右上详情面板的性质图标：单击固定/取消右下说明
        if self.detail_panel.contains(pos):
            aid = self.detail_panel.aspect_at(pos)
            if aid is not None:
                self.pinned_aspect = None if self.pinned_aspect == aid else aid
            return
        # 卡牌（取最上层：绘制顺序靠后的优先）
        for uid in reversed(list(self.card_views)):
            v = self.card_views[uid]
            if v.visible and v.screen_rect(self.cam).collidepoint(pos):
                if v.data["location"].get("kind") in ("table", "verb"):
                    self._press = {"kind": "card", "uid": uid, "pos": pos,
                                   "last": pos, "moved": False,
                                   "from": v.data["location"]["kind"]}
                    v.dragging = True
                return
        # 行动槽
        for uid, vv in self.verb_views.items():
            if vv.screen_rect(self.cam).collidepoint(pos):
                self._press = {"kind": "verb", "uid": uid, "pos": pos,
                               "last": pos, "moved": False}
                vv.dragging = True
                return
        # 空白处：平移桌面的候选
        self._press = {"kind": "empty", "uid": None, "pos": pos,
                       "last": pos, "moved": False}

    def _on_drag_motion(self, pos):
        p = self._press
        if p is None:
            return
        if not p["moved"]:
            dx, dy = pos[0] - p["pos"][0], pos[1] - p["pos"][1]
            if dx * dx + dy * dy > CLICK_MAX_DIST ** 2:
                p["moved"] = True
        if p["moved"]:
            step_x = (pos[0] - p["last"][0]) / self.cam.zoom
            step_y = (pos[1] - p["last"][1]) / self.cam.zoom
            if p["kind"] == "verb":
                vv = self.verb_views[p["uid"]]
                vv.x += step_x
                vv.y += step_y
            elif p["kind"] == "empty":
                self.cam.pan_pixels(pos[0] - p["last"][0], pos[1] - p["last"][1])
            # 卡牌的跟随在 update() 里做（带平滑）
        p["last"] = pos

    def _on_left_up(self, pos):
        """松开：位移小 = 单击/双击；位移大 = 拖放落点。"""
        p = self._press
        if p is None:
            return
        self._press = None
        if p["kind"] == "card":
            self.card_views[p["uid"]].dragging = False
        elif p["kind"] == "verb":
            self.verb_views[p["uid"]].dragging = False

        if not p["moved"]:
            self._click(p)
        elif p["kind"] == "card":
            self._drop_card(p["uid"], pos, p["from"])
        elif p["kind"] == "verb":
            vv = self.verb_views[p["uid"]]
            try:
                self.game.move_verb(p["uid"],
                                    int(round(vv.x / C.CELL)),
                                    int(round(vv.y / C.CELL)))
            except GameError as e:
                self.toast(str(e))
            self.sync()

    def _click(self, p: dict):
        if p["kind"] == "card":
            now = time.time()
            last_t, last_uid = self._last_click
            if last_uid == p["uid"] and (now - last_t) * 1000 < DBL_CLICK_MS:
                self._last_click = (0.0, None)
                self._quick_fill(p["uid"])          # 双击：快速入槽
            else:
                self._last_click = (now, p["uid"])
                self._sticky_card = p["uid"]        # 单击：打开卡牌详情
        elif p["kind"] == "verb":
            self.event_panel.open_for(p["uid"])     # 单击：打开事件面板
        else:
            self.event_panel.close()
            self._sticky_card = None

    def _quick_fill(self, card_uid: str):
        """双击卡牌：填入距离最近的行动槽里第一个能接纳它的槽位。

        槽有运行中的局面则优先尝试其子槽位；空闲槽则进主卡口。
        """
        view = self.card_views.get(card_uid)
        if view is None:
            return

        def dist2(v) -> float:
            cx = (v["x"] + v["w"] / 2) * C.CELL
            cy = (v["y"] + v["h"] / 2) * C.CELL
            return (cx - view.x) ** 2 + (cy - view.y) ** 2

        for v in sorted(self.snapshot["verbs"], key=dist2):
            if v["situation"]:
                sit = self._situation_of(v["uid"])
                for s in (sit["slots"] if sit else []):
                    if len(s["cards"]) >= s["capacity"]:
                        continue
                    try:
                        self.game.fill_slot(sit["uid"], s["id"], card_uid)
                        self.toast(f"已填入「{v['label']}」的{s['label']}")
                        self.sync()
                        self._check_ending()
                        return
                    except GameError:
                        continue
            else:
                try:
                    self.game.put_in_verb(v["uid"], card_uid)
                    self.toast(f"已放入「{v['label']}」")
                    self.sync()
                    return
                except GameError:
                    continue
        self.toast("附近没有能接纳它的槽位")

    def _drop_card(self, uid: str, pos, from_kind: str):
        """拖放落点三级判定：面板子槽位 > 空闲行动槽 > 桌面网格。"""
        try:
            slot = self.event_panel.slot_at(pos) if self.event_panel.shown else None
            if slot is not None:
                sit = self._situation_of(self.event_panel.verb_uid)
                if sit:
                    self.game.fill_slot(sit["uid"], slot, uid)
            else:
                wx, wy = self.cam.screen_to_world(*pos)
                verb_uid = None
                for v_uid, vv in self.verb_views.items():
                    if vv.screen_rect(self.cam).collidepoint(pos):
                        verb_uid = v_uid
                        break
                if verb_uid is not None and \
                        self.verbs_by_uid()[verb_uid]["situation"] is None:
                    if from_kind == "table":
                        self.game.put_in_verb(verb_uid, uid)
                else:
                    if from_kind == "verb":
                        old = self.card_views[uid].data["location"].get("verb")
                        self.game.take_from_verb(old, uid)
                    self.game.move_card(uid, int(wx // C.CELL), int(wy // C.CELL))
        except GameError as e:
            self.toast(str(e))
        self.sync()
        self._check_ending()

    def _zoom(self, factor: float):
        w, h = self.app.screen.get_size()
        self.cam.zoom_at(w / 2, h / 2, factor)

    def _on_pinch(self, ev):
        """双指捏合缩放（依赖平台派发 MULTIGESTURE；没有就用 ± 按钮）。"""
        d = getattr(ev, "d_dist", None)
        if d is None:
            d = getattr(ev, "pinched", None)
        if d is None:
            return
        self._zoom(max(0.5, min(2.0, 1.0 + d)))

    def _end_turn(self):
        try:
            self.game.end_turn()
        except GameError as e:
            self.toast(str(e))
        self.sync()
        self._check_ending()

    def _open_pause_menu(self):
        w, h = self.app.screen.get_size()
        m = pygame_menu.Menu("暂停", w, h, theme=_menu_theme())
        m.add.button("继续", lambda: setattr(self, "pause_menu", None))
        m.add.button("保存", self._save)
        m.add.button("回到主菜单", self.app.to_menu)
        self.pause_menu = m

    def _save(self):
        with open(C.SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.game.to_dict(), f, ensure_ascii=False)
        self.toast("已保存")
        self.pause_menu = None

    def _handle_ending(self, ev):
        if ev.type == pygame.MOUSEMOTION:
            for b in self._ending_buttons:
                b.hover = b.rect.collidepoint(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            for b in self._ending_buttons:
                if b.rect.collidepoint(ev.pos):
                    e = self.content.endings.get(self.ending_id)
                    if b.action == "legacy":
                        self.app.start_game(legacy_spawn=dict(e.legacy_spawn) if e else None)
                    else:
                        self.app.to_menu()

    # ==================== 帧更新与绘制 ====================

    def update(self, dt: float):
        if self.pause_menu is not None:
            return
        mx, my = pygame.mouse.get_pos()
        # 拖拽中的卡跟手（带平滑，手感更润）
        if self._press and self._press["kind"] == "card" and self._press["moved"]:
            v = self.card_views.get(self._press["uid"])
            if v is not None:
                wx, wy = self.cam.screen_to_world(mx, my)
                k = min(1.0, C.LERP * 1.6 * dt)
                v.x += (wx - v.x) * k
                v.y += (wy - v.y) * k
        # 悬停检测（鼠标下的卡牌发光）
        self.hover_uid = None
        for uid in reversed(list(self.card_views)):
            v = self.card_views[uid]
            v.hover = False
            if v.visible and v.screen_rect(self.cam).collidepoint((mx, my)):
                v.hover = True
                self.hover_uid = uid
                break
        # 详情面板粘性：单击打开的卡保持显示；鼠标移到面板自己身上不解除
        if self._sticky_card not in self.card_views:
            self._sticky_card = None
        # 行动槽悬停
        for vv in self.verb_views.values():
            vv.hover = vv.screen_rect(self.cam).collidepoint((mx, my))
        for t in self.toasts:
            t[1] -= dt
        self.toasts = [t for t in self.toasts if t[1] > 0]
        for v in self.card_views.values():
            v.update(dt)
        for vv in self.verb_views.values():
            vv.update(dt)
        self.event_panel.update(dt)
        self.end_turn_btn.hover = self.end_turn_btn.rect.collidepoint((mx, my))
        self.zoom_in_btn.hover = self.zoom_in_btn.rect.collidepoint((mx, my))
        self.zoom_out_btn.hover = self.zoom_out_btn.rect.collidepoint((mx, my))

    def draw(self, surf: pygame.Surface):
        t = pygame.time.get_ticks() / 1000.0
        self._draw_background(surf)
        self._draw_grid(surf)

        # 行动槽
        for v in self.snapshot["verbs"]:
            view = self.verb_views[v["uid"]]
            sit = next((s for s in self.snapshot["situations"]
                        if s["uid"] == v["situation"]), None)
            view.draw(surf, self.cam, t, sit)

        # 卡牌（拖拽中的最后画，浮在最上）
        order = [v for v in self.card_views.values()
                 if v.visible and not v.dragging]
        order += [v for v in self.card_views.values() if v.dragging]
        for v in order:
            v.draw(surf, self.cam, t, self.aspect_colors)

        # HUD：回合计数 + 结束回合 + 缩放按钮
        sh = surf.get_height()
        surf.blit(C.font(18, bold=True).render(
            f"回合 {self.snapshot['turn']}", True, C.GOLD), (18, 14))
        self.end_turn_btn.rect.topleft = (16, sh - 56)
        self.zoom_out_btn.rect.topleft = (168, sh - 56)
        self.zoom_in_btn.rect.topleft = (220, sh - 56)
        self.end_turn_btn.draw(surf)
        self.zoom_out_btn.draw(surf)
        self.zoom_in_btn.draw(surf)

        # 面板三件套
        self.event_panel.draw(surf)
        sticky = self.card_views.get(self._sticky_card) if self._sticky_card else None
        self.detail_panel.draw(surf, sticky.data if sticky else None)
        self.aspect_panel.draw(surf, self.pinned_aspect)

        # 浮动提示
        y = sh - 110
        for msg, _ in self.toasts[-3:]:
            img = C.font(14).render(msg, True, C.WHITE)
            bg = img.get_rect(center=(surf.get_width() // 2, y)).inflate(24, 12)
            s = pygame.Surface(bg.size, pygame.SRCALPHA)
            pygame.draw.rect(s, (*C.PANEL, 220), s.get_rect(), border_radius=8)
            surf.blit(s, bg.topleft)
            surf.blit(img, img.get_rect(center=bg.center))
            y -= 34

        # 结局演出
        if self.ending_id is not None:
            self._draw_ending(surf)
        # 暂停菜单
        if self.pause_menu is not None:
            self.pause_menu.draw(surf)

    def _draw_background(self, surf: pygame.Surface):
        if self.bg is None:
            surf.fill(C.INK)
            return
        # 背景贴图平铺，随相机轻微视差（0.3 倍速）
        bw, bh = self.bg.get_size()
        ox = int(-self.cam.x * self.cam.zoom * 0.3) % bw - bw
        oy = int(-self.cam.y * self.cam.zoom * 0.3) % bh - bh
        for x in range(ox, surf.get_width() + bw, bw):
            for y in range(oy, surf.get_height() + bh, bh):
                surf.blit(self.bg, (x, y))

    def _draw_grid(self, surf: pygame.Surface):
        step = C.CELL * self.cam.zoom
        if step < 9:  # 缩得太小就不画网格了
            return
        w, h = surf.get_size()
        x0, y0 = self.cam.screen_to_world(0, 0)
        x1, y1 = self.cam.screen_to_world(w, h)
        color = (255, 255, 255, 14)
        grid = pygame.Surface((w, h), pygame.SRCALPHA)
        gx = math.floor(x0 / C.CELL) * C.CELL
        while gx <= x1:
            sx, _ = self.cam.world_to_screen(gx, 0)
            pygame.draw.line(grid, color, (sx, 0), (sx, h))
            gx += C.CELL
        gy = math.floor(y0 / C.CELL) * C.CELL
        while gy <= y1:
            _, sy = self.cam.world_to_screen(0, gy)
            pygame.draw.line(grid, color, (0, sy), (w, sy))
            gy += C.CELL
        surf.blit(grid, (0, 0))

    def _draw_ending(self, surf: pygame.Surface):
        e = self.content.endings.get(self.ending_id)
        dim = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
        dim.fill((6, 8, 14, 215))
        surf.blit(dim, (0, 0))
        cx, cy = surf.get_width() // 2, surf.get_height() // 2
        title = C.font(40, bold=True).render(e.label if e else "结局", True, C.GOLD)
        surf.blit(title, title.get_rect(center=(cx, cy - 120)))
        for i, line in enumerate(C.wrap_text(e.text if e else "", C.font(17), 560)):
            img = C.font(17).render(line, True, C.WHITE)
            surf.blit(img, img.get_rect(center=(cx, cy - 60 + i * 26)))
        for b in self._ending_buttons:
            b.draw(surf)