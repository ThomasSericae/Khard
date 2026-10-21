"""引擎核：回合制结算流水线与前端 API。

设计要点：
- 事件（RecipeDef）是状态机而非一次性函数；Situation 是其执行实例，
  可通过 linked 显式移交控制权，零回合配方在结算队列中同步链式跑完。
- 链中需要玩家决策时，Situation 挂起为 awaiting_input，
  前端填槽后调 commit() 从断点恢复，或调 abort() 放弃。
- 本模块不 import 任何图形库；Game 状态可整体 JSON 序列化（即存档）。
- 只有桌面上的卡参与衰变；被局面关押的卡不 decay（与原作一致）。
"""
from __future__ import annotations

import random
from dataclasses import asdict

from .model import (ContentBase, SlotDef, RecipeDef, VerbDef,
                    CardInstance, VerbInstance, Situation, DeckInstance)


class GameError(Exception):
    """前端可捕获的操作错误（如往被占用的槽里放卡、提交未填满的必需槽）。"""


class Game:
    # ==================== 构造 ====================

    def __init__(self, content: ContentBase, seed: int | None = None,
                 legacy_spawn: dict[str, int] | None = None):
        self.content = content
        self.rng = random.Random(seed)
        self.turn = 0
        self.over: str | None = None      # 结局 id；None = 进行中
        self.log: list[str] = []          # 事件流水，供调试
        self._uid_counter = 0
        self.cards: dict[str, CardInstance] = {}
        self.verbs: dict[str, VerbInstance] = {}
        self.situations: dict[str, Situation] = {}
        self.decks: dict[str, DeckInstance] = {}
        self._queue: list[str] = []           # 待结算的 Situation uid
        self._spawned_once: set[str] = set()  # 已触发的一次性生成条件（VerbDef id）

        for deck_id, d in content.decks.items():
            queue = [cid for cid, n in d.cards.items() for _ in range(n)]
            self.rng.shuffle(queue)
            self.decks[deck_id] = DeckInstance(def_id=deck_id, queue=queue)
        for vd in content.verbs.values():
            if vd.spawn_condition is None:
                self._spawn_verb(vd)
        for card_id, n in (legacy_spawn or {}).items():
            self.spawn_card(card_id, n)
        self._drain_queue()

    def _new_uid(self, prefix: str) -> str:
        self._uid_counter += 1
        return f"{prefix}{self._uid_counter}"

    def _elog(self, msg: str):
        self.log.append(f"[回合{self.turn}] {msg}")

    # ==================== 网格与桌面 ====================

    def _table_cells(self) -> dict[tuple[int, int], str]:
        """桌面占用表：{(x, y): 卡 uid}。"""
        return {(c.location["x"], c.location["y"]): uid
                for uid, c in self.cards.items() if c.location.get("kind") == "table"}

    def _verb_cells(self) -> set[tuple[int, int]]:
        """所有行动槽占据的格子集合。"""
        cells = set()
        for v in self.verbs.values():
            vd = self.content.verbs[v.def_id]
            for dx in range(vd.grid_w):
                for dy in range(vd.grid_h):
                    cells.add((v.x + dx, v.y + dy))
        return cells

    def _free_cell(self, near: tuple[int, int] = (0, 0)) -> tuple[int, int]:
        """从参考点按方环向外找第一个空格。"""
        blocked = set(self._table_cells()) | self._verb_cells()
        x0, y0 = near
        for r in range(64):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    if (x0 + dx, y0 + dy) not in blocked:
                        return (x0 + dx, y0 + dy)
        raise GameError("桌面已满")

    def _free_rect(self, w: int, h: int) -> tuple[int, int]:
        """为新行动槽找一块 w×h 的空地。"""
        blocked = set(self._table_cells()) | self._verb_cells()
        for r in range(64):
            for x0 in range(-r, r + 1):
                for y0 in range(-r, r + 1):
                    if max(abs(x0), abs(y0)) != r:
                        continue
                    if all((x0 + dx, y0 + dy) not in blocked
                           for dx in range(w) for dy in range(h)):
                        return (x0, y0)
        raise GameError("桌面已满")

    # ==================== 生成与消灭 ====================

    def spawn_card(self, def_id: str, count: int = 1,
                   near: tuple[int, int] = (0, 0)) -> CardInstance:
        """在桌面生成卡。可堆叠卡优先并入既有牌堆；唯一卡已存在则直接返回它。

        :param def_id: CardDef id
        :param count: 生成张数
        :param near: 放置参考点（就近找空格）
        """
        d = self.content.cards[def_id]
        if d.unique:
            for c in self.cards.values():
                if c.def_id == def_id:
                    return c
        if d.stackable and not d.unique:
            for c in self.cards.values():
                if c.def_id == def_id and c.location.get("kind") == "table":
                    c.stack += count
                    return c
        x, y = self._free_cell(near)
        c = CardInstance(uid=self._new_uid("c"), def_id=def_id, stack=count,
                         remaining_life=d.lifetime,
                         location={"kind": "table", "x": x, "y": y})
        self.cards[c.uid] = c
        return c

    def _consume_card(self, uid: str):
        self.cards.pop(uid, None)

    def _spawn_verb(self, vd: VerbDef) -> VerbInstance:
        x, y = self._free_rect(vd.grid_w, vd.grid_h)
        v = VerbInstance(uid=self._new_uid("v"), def_id=vd.id, x=x, y=y,
                         remaining_life=vd.lifetime)
        self.verbs[v.uid] = v
        self._elog(f"行动槽「{vd.label}」出现")
        if vd.auto and vd.default_recipe:
            self._begin_recipe(v, self.content.recipes[vd.default_recipe], [])
        return v

    # ==================== 性质与条件 ====================

    def _pool(self, uids: list[str]) -> dict[str, int]:
        """汇总一组卡的性质点数与卡种计数。

        卡种条件用带前缀的键 "card:<卡id>"，与性质严格分命名空间——
        否则一张与某性质同名的卡会被双重计数（性质值 + 卡种数）。
        """
        pool: dict[str, int] = {}
        for uid in uids:
            c = self.cards[uid]
            d = self.content.cards[c.def_id]
            for k, v in d.aspects.items():
                pool[k] = pool.get(k, 0) + v * c.stack
            key = f"card:{c.def_id}"
            pool[key] = pool.get(key, 0) + c.stack
        return pool

    @staticmethod
    def _met(requirements: dict[str, int], pool: dict[str, int]) -> bool:
        """正值要求 pool 中该键 ≥ 值；零或负值要求该键不存在。"""
        for k, v in requirements.items():
            have = pool.get(k, 0)
            if v > 0 and have < v:
                return False
            if v <= 0 and have > 0:
                return False
        return True

    def _card_fits_slot(self, card: CardInstance, slot: SlotDef) -> bool:
        pool = self._pool([card.uid])
        if not self._met(slot.required, pool):
            return False
        return all(pool.get(k, 0) <= 0 for k in slot.forbidden)

    def _situation_pool(self, sit: Situation) -> dict[str, int]:
        uids = list(sit.main)
        for bound in sit.slots.values():
            uids.extend(bound)
        return self._pool(uids)

    # ==================== 前端 API：桌面操作 ====================

    def move_card(self, uid: str, x: int, y: int):
        """拖动桌面卡到 (x, y)；落点是同种可堆叠卡则合并，被异种卡占用则报错。

        :param uid: 卡实例 uid
        :param x: 目标格横坐标
        :param y: 目标格纵坐标
        """
        c = self.cards[uid]
        if c.location.get("kind") != "table":
            raise GameError("只能拖动桌面上的卡")
        if (x, y) in self._verb_cells():
            raise GameError("目标格子被行动槽占用")
        other = self._table_cells().get((x, y))
        if other and other != uid:
            d, od = self.content.cards[c.def_id], self.content.cards[self.cards[other].def_id]
            if c.def_id == self.cards[other].def_id and d.stackable and not d.unique:
                self.cards[other].stack += c.stack
                self._consume_card(uid)
                return
            raise GameError("目标格子被占用")
        c.location = {"kind": "table", "x": x, "y": y}

    def move_verb(self, verb_uid: str, x: int, y: int):
        """拖动行动槽到网格 (x, y)。不允许与其他行动槽重叠，允许压在卡牌上。

        :param verb_uid: 槽实例 uid
        :param x: 目标格横坐标
        :param y: 目标格纵坐标
        """
        v = self.verbs[verb_uid]
        vd = self.content.verbs[v.def_id]
        for o in self.verbs.values():
            if o.uid == verb_uid:
                continue
            od = self.content.verbs[o.def_id]
            if (x < o.x + od.grid_w and o.x < x + vd.grid_w
                    and y < o.y + od.grid_h and o.y < y + vd.grid_h):
                raise GameError("目标位置与其他行动槽重叠")
        v.x, v.y = x, y

    def put_in_verb(self, verb_uid: str, card_uid: str):
        """把桌面卡放进空闲行动槽的主卡口。"""
        v = self.verbs[verb_uid]
        if v.situation:
            raise GameError("该行动槽正在运行事件")
        c = self.cards[card_uid]
        if c.location.get("kind") != "table":
            raise GameError("只能从桌面取卡")
        c.location = {"kind": "verb", "verb": verb_uid}

    def take_from_verb(self, verb_uid: str, card_uid: str):
        """把主卡口里的卡拿回桌面（事件未开始时）。"""
        c = self.cards[card_uid]
        if c.location.get("kind") != "verb" or c.location["verb"] != verb_uid:
            raise GameError("这张卡不在该槽的主卡口")
        v = self.verbs[verb_uid]
        c.location = {"kind": "table", "x": v.x, "y": v.y + 2}
        c.location["x"], c.location["y"] = self._free_cell((v.x, v.y + 2))

    # ==================== 前端 API：事件的发起与交互 ====================

    def preview(self, verb_uid: str) -> list[dict]:
        """预览当前主卡口内容能触发哪些配方（前端显示 start_text 用）。"""
        v = self.verbs[verb_uid]
        main = [uid for uid, c in self.cards.items()
                if c.location.get("kind") == "verb" and c.location["verb"] == verb_uid]
        return [{"id": r.id, "label": r.label, "start_text": r.start_text}
                for r in self._candidates(v, main)]

    def begin(self, verb_uid: str) -> str:
        """启动事件：按主卡口内容匹配配方，返回 Situation uid。

        主卡口为空时使用槽的默认配方；多个候选取第一个并记日志（内容应尽量互斥）。
        """
        v = self.verbs[verb_uid]
        if v.situation:
            raise GameError("该行动槽正在运行事件")
        main = [uid for uid, c in self.cards.items()
                if c.location.get("kind") == "verb" and c.location["verb"] == verb_uid]
        candidates = self._candidates(v, main)
        if not candidates:
            raise GameError("没有满足条件的事件")
        if len(candidates) > 1:
            self._elog(f"警告：{len(candidates)} 个候选配方 { [r.id for r in candidates] }，取第一个")
        return self._begin_recipe(v, candidates[0], main)

    def fill_slot(self, sit_uid: str, slot_id: str, card_uid: str):
        """把桌面卡填入局面的子槽位（即时决策时玩家这么放卡）。

        :param sit_uid: Situation uid
        :param slot_id: 子槽位 id
        :param card_uid: 桌面上的卡 uid
        """
        sit = self.situations[sit_uid]
        if sit.phase not in ("awaiting_input", "running"):
            raise GameError("该局面当前不接受输入")
        recipe = self.content.recipes[sit.recipe]
        sdef = next((s for s in recipe.slots if s.id == slot_id), None)
        if sdef is None:
            raise GameError(f"当前事件没有槽位 {slot_id}")
        if len(sit.slots.setdefault(slot_id, [])) >= sdef.capacity:
            raise GameError("槽位已满")
        c = self.cards[card_uid]
        if c.location.get("kind") != "table":
            raise GameError("只能从桌面取卡")
        if not self._card_fits_slot(c, sdef):
            raise GameError("这张卡不满足槽位要求")
        sit.slots[slot_id].append(card_uid)
        c.location = {"kind": "slot", "situation": sit_uid, "slot": slot_id}

    def unfill_slot(self, sit_uid: str, slot_id: str, card_uid: str):
        """把子槽位里的卡拿回桌面。"""
        sit = self.situations[sit_uid]
        if card_uid not in sit.slots.get(slot_id, []):
            raise GameError("这张卡不在该槽位")
        sit.slots[slot_id].remove(card_uid)
        v = self.verbs[sit.verb]
        x, y = self._free_cell((v.x, v.y + 2))
        self.cards[card_uid].location = {"kind": "table", "x": x, "y": y}

    def commit(self, sit_uid: str):
        """玩家完成即时决策后提交：从挂起断点恢复执行链。"""
        sit = self.situations[sit_uid]
        if sit.phase != "awaiting_input":
            raise GameError("该局面并未等待输入")
        recipe = self.content.recipes[sit.recipe]
        missing = [s.id for s in recipe.slots if s.essential and not sit.slots.get(s.id)]
        if missing:
            raise GameError(f"必需槽位未填：{missing}（或调用 abort 放弃）")
        sit.phase = "running"
        self._execute(sit)
        self._drain_queue()

    def abort(self, sit_uid: str):
        """放弃一个等待输入的局面：全部卡牌退回桌面，事件链中断。"""
        sit = self.situations[sit_uid]
        if sit_uid in self._queue:
            self._queue.remove(sit_uid)
        self._elog(f"玩家放弃了「{self.content.recipes[sit.recipe].label}」")
        self._finish(sit)

    # ==================== 回合结算流水线 ====================

    def end_turn(self):
        """推进一个回合。这是引擎唯一的时间入口。"""
        if self.over:
            raise GameError("游戏已结束")
        self.turn += 1
        self._decay_cards()
        self._expire_verbs()
        self._spawn_verbs()
        self._tick_situations()
        self._drain_queue()

    def _decay_cards(self):
        """桌面卡寿命 -1；归零执行衰变链（decay_to）或消失。关押中的卡不衰变。"""
        for uid, c in list(self.cards.items()):
            if c.location.get("kind") != "table" or c.remaining_life is None:
                continue
            c.remaining_life -= 1
            if c.remaining_life > 0:
                continue
            d = self.content.cards[c.def_id]
            if d.decay_to:
                c.def_id = d.decay_to
                c.remaining_life = self.content.cards[d.decay_to].lifetime
                self._elog(f"「{d.label}」衰变为「{self.content.cards[d.decay_to].label}」")
            else:
                self._elog(f"「{d.label}」消散了")
                self._consume_card(uid)

    def _expire_verbs(self):
        """行动槽寿命 -1；归零则中断其中事件并移除。"""
        for uid, v in list(self.verbs.items()):
            if v.remaining_life is None:
                continue
            v.remaining_life -= 1
            if v.remaining_life > 0:
                continue
            if v.situation and v.situation in self.situations:
                self._finish(self.situations[v.situation])
            for c in self.cards.values():
                if c.location.get("kind") == "verb" and c.location["verb"] == uid:
                    x, y = self._free_cell((v.x, v.y + 2))
                    c.location = {"kind": "table", "x": x, "y": y}
            self._elog(f"行动槽「{self.content.verbs[v.def_id].label}」关闭了")
            del self.verbs[uid]

    def _spawn_verbs(self):
        """检查生成条件，产生新行动槽（心跳就是 every_n_turns 的自动槽）。"""
        for vd in self.content.verbs.values():
            cond = vd.spawn_condition
            if cond is None:
                continue
            live = any(v.def_id == vd.id for v in self.verbs.values())
            t = cond.get("type")
            if t == "every_n_turns":
                if self.turn % cond["n"] == 0 and not live:
                    self._spawn_verb(vd)
            elif t == "has_card":
                if vd.id not in self._spawned_once and not live and \
                        any(c.def_id == cond["card"] for c in self.cards.values()):
                    self._spawned_once.add(vd.id)
                    self._spawn_verb(vd)
            elif t == "on_turn":
                if self.turn == cond["turn"] and vd.id not in self._spawned_once:
                    self._spawned_once.add(vd.id)
                    self._spawn_verb(vd)

    def _tick_situations(self):
        """推进运行中的局面：替代配方掷骰切换；到达 warmup 的进结算队列。"""
        for sit in list(self.situations.values()):
            if sit.phase != "running":
                continue
            recipe = self.content.recipes[sit.recipe]
            sit.elapsed += 1
            for alt in recipe.alternatives:
                ar = self.content.recipes[alt["recipe"]]
                if self._met(ar.requirements, self._situation_pool(sit)) \
                        and self.rng.random() * 100 < alt.get("chance", 100):
                    self._elog(f"「{recipe.label}」切换为「{ar.label}」")
                    self._transfer(sit, ar)
                    recipe = ar
                    break
            if sit.elapsed >= recipe.warmup:
                self._queue.append(sit.uid)

    def _drain_queue(self):
        """依次结算队列中的局面；挂起为 awaiting_input 的不阻塞其他局面。"""
        while self._queue and not self.over:
            sit = self.situations.get(self._queue.pop(0))
            if sit is None:
                continue
            self._execute(sit)

    # ==================== Situation 状态机 ====================

    def _candidates(self, verb: VerbInstance, main: list[str]) -> list[RecipeDef]:
        vd = self.content.verbs[verb.def_id]
        pool = self._pool(main)
        cands = [r for r in self.content.recipes.values()
                 if r.verb == vd.id and r.craftable and self._met(r.requirements, pool)]
        if not main and vd.default_recipe:  # 空槽优先默认配方
            cands.sort(key=lambda r: r.id != vd.default_recipe)
        return cands

    def _begin_recipe(self, verb: VerbInstance, recipe: RecipeDef,
                      main: list[str]) -> str:
        sit = Situation(uid=self._new_uid("s"), verb=verb.uid, recipe=recipe.id,
                        main=list(main), chain=[recipe.id])
        self.situations[sit.uid] = sit
        verb.situation = sit.uid
        for uid in main:
            self.cards[uid].location = {"kind": "main", "situation": sit.uid}
        self._init_slots(sit, recipe)
        self._elog(f"事件「{recipe.label}」开始")
        if recipe.warmup > 0:
            sit.phase = "running"
        else:
            self._queue.append(sit.uid)
            self._drain_queue()
        return sit.uid

    def _init_slots(self, sit: Situation, recipe: RecipeDef):
        """按配方声明初始化子槽位，并执行贪婪槽的自动吸取。"""
        sit.slots = {s.id: [] for s in recipe.slots}
        for s in recipe.slots:
            if not s.greedy:
                continue
            for c in list(self.cards.values()):
                if len(sit.slots[s.id]) >= s.capacity:
                    break
                if c.location.get("kind") == "table" and self._card_fits_slot(c, s):
                    sit.slots[s.id].append(c.uid)
                    c.location = {"kind": "slot", "situation": sit.uid, "slot": s.id}
                    self._elog(f"贪婪槽「{s.label}」吸取了「{self.content.cards[c.def_id].label}」")

    def _execute(self, sit: Situation):
        """执行当前配方：必需槽未满则挂起等待输入；否则结算效果并沿 linked 链移交。

        零回合配方在 while 循环里同步链式跑完，直到链尽、挂起或转入耗回合配方。
        """
        while True:
            recipe = self.content.recipes[sit.recipe]
            missing = [s for s in recipe.slots if s.essential and not sit.slots.get(s.id)]
            if missing:
                sit.phase = "awaiting_input"
                self._elog(f"「{recipe.label}」等待输入：{[s.label for s in missing]}")
                return
            self._apply_effects(sit, recipe)
            if self.over:
                return
            nxt = None
            pool = self._situation_pool(sit)
            for rid in recipe.linked:
                rdef = self.content.recipes[rid]
                if self._met(rdef.requirements, pool):
                    nxt = rdef
                    break
            if nxt is None:
                self._finish(sit)
                return
            self._transfer(sit, nxt)
            if nxt.warmup > 0:
                sit.phase = "running"
                return
            # 零回合配方：循环继续，同步执行

    def _transfer(self, sit: Situation, rdef: RecipeDef):
        """移交控制权：旧槽位解散，其中卡牌并入主卡口；初始化新配方槽位。"""
        for bound in sit.slots.values():
            for uid in bound:
                self.cards[uid].location = {"kind": "main", "situation": sit.uid}
                sit.main.append(uid)
        sit.recipe = rdef.id
        sit.elapsed = 0
        sit.chain.append(rdef.id)
        self._init_slots(sit, rdef)
        self._elog(f"控制权移交「{rdef.label}」")

    def _finish(self, sit: Situation):
        """局面结束：所有关押卡牌退回桌面（可堆叠的并入牌堆），槽位释放。"""
        v = self.verbs.get(sit.verb)
        near = (v.x, v.y + 2) if v else (0, 0)
        for uid in list(sit.main) + [u for b in sit.slots.values() for u in b]:
            c = self.cards.get(uid)
            if c is None:
                continue
            d = self.content.cards[c.def_id]
            merged = False
            if d.stackable and not d.unique:
                for o in self.cards.values():
                    if o is not c and o.def_id == c.def_id \
                            and o.location.get("kind") == "table":
                        o.stack += c.stack
                        self._consume_card(uid)
                        merged = True
                        break
            if not merged:
                x, y = self._free_cell(near)
                c.location = {"kind": "table", "x": x, "y": y}
        if v and v.situation == sit.uid:
            v.situation = None
        self._elog(f"事件「{self.content.recipes[sit.recipe].label}」结束"
                   f"（链：{' → '.join(sit.chain)}）")
        del self.situations[sit.uid]

    # ==================== 效果原语 ====================

    def _apply_effects(self, sit: Situation, recipe: RecipeDef):
        """解释配方的 effects 列表。支持的原语：

        {"op": "consume_slot",   "slot": sid}              吞噬某子槽的卡
        {"op": "consume_main"}                              吞噬主卡口的卡
        {"op": "consume_aspect", "aspect": a, "count": n}   按性质点数吞噬局面内的卡
        {"op": "transform", "slot": sid, "card": def_id}    把槽内卡转化为另一卡种
        {"op": "spawn", "card": def_id, "count": n}         在桌面生卡
        {"op": "draw",  "deck": deck_id, "times": n}        从卡组抽卡到桌面
        {"op": "ending", "id": ending_id}                   触发结局
        """
        v = self.verbs.get(sit.verb)
        near = (v.x, v.y + 2) if v else (0, 0)
        for eff in recipe.effects:
            op = eff["op"]
            if op == "consume_slot":
                for uid in sit.slots.get(eff["slot"], []):
                    self._consume_card(uid)
                sit.slots[eff["slot"]] = []
            elif op == "consume_main":
                for uid in list(sit.main):
                    self._consume_card(uid)
                sit.main = []
            elif op == "consume_aspect":
                self._consume_aspect(sit, eff["aspect"], eff.get("count", 1))
            elif op == "transform":
                for uid in sit.slots.get(eff["slot"], []):
                    c = self.cards[uid]
                    c.def_id = eff["card"]
                    c.remaining_life = self.content.cards[eff["card"]].lifetime
            elif op == "spawn":
                self.spawn_card(eff["card"], eff.get("count", 1), near)
            elif op == "draw":
                for _ in range(eff.get("times", 1)):
                    self._draw(eff["deck"], near)
            elif op == "ending":
                self.over = eff["id"]
                e = self.content.endings.get(self.over)
                self._elog(f"结局达成：{e.label if e else self.over}")
            else:
                raise GameError(f"未知效果原语：{op}")

    def _consume_aspect(self, sit: Situation, aspect: str, count: int):
        """从局面关押的卡里按性质点数吞噬，先主卡口后槽位。"""
        need = count
        uids = list(sit.main) + [u for b in sit.slots.values() for u in b]
        for uid in list(uids):
            if need <= 0:
                break
            c = self.cards.get(uid)
            if c is None:
                continue
            pts = self.content.cards[c.def_id].aspects.get(aspect, 0)
            while c.stack > 0 and need > 0 and pts > 0:
                c.stack -= 1
                need -= pts
            if c.stack <= 0:
                self._consume_card(uid)
                if uid in sit.main:
                    sit.main.remove(uid)
                for b in sit.slots.values():
                    if uid in b:
                        b.remove(uid)

    def _draw(self, deck_id: str, near: tuple[int, int]):
        inst = self.decks[deck_id]
        if not inst.queue:
            if self.content.decks[deck_id].reset_on_empty:
                inst.queue = [cid for cid, n in self.content.decks[deck_id].cards.items()
                              for _ in range(n)]
                self.rng.shuffle(inst.queue)
                self._elog(f"卡组 {deck_id} 重置洗牌")
            else:
                return
        cid = inst.queue.pop()
        self._elog(f"从卡组 {deck_id} 抽到「{self.content.cards[cid].label}」")
        self.spawn_card(cid, 1, near)

    # ==================== 快照与存档 ====================

    def snapshot(self) -> dict:
        """给前端的状态快照：前端不需要接触内容库即可渲染。"""
        cards = []
        for c in self.cards.values():
            d = self.content.cards[c.def_id]
            cards.append({"uid": c.uid, "def": c.def_id, "label": d.label,
                          "icon": d.icon, "aspects": d.aspects, "stack": c.stack,
                          "life": c.remaining_life, "location": c.location})
        verbs = []
        for v in self.verbs.values():
            vd = self.content.verbs[v.def_id]
            verbs.append({"uid": v.uid, "def": v.def_id, "label": vd.label,
                          "icon": vd.icon, "x": v.x, "y": v.y,
                          "w": vd.grid_w, "h": vd.grid_h,
                          "life": v.remaining_life, "situation": v.situation})
        sits = []
        for s in self.situations.values():
            r = self.content.recipes[s.recipe]
            sits.append({"uid": s.uid, "verb": s.verb, "recipe": s.recipe,
                         "label": r.label, "phase": s.phase,
                         "elapsed": s.elapsed, "warmup": r.warmup,
                         "start_text": r.start_text, "end_text": r.end_text,
                         "main": list(s.main),
                         "slots": [{"id": sd.id, "label": sd.label,
                                    "required": sd.required, "greedy": sd.greedy,
                                    "essential": sd.essential, "capacity": sd.capacity,
                                    "cards": list(s.slots.get(sd.id, []))}
                                   for sd in r.slots]})
        return {"turn": self.turn, "over": self.over, "cards": cards,
                "verbs": verbs, "situations": sits,
                "decks": [{"def": d.def_id, "remaining": len(d.queue)}
                          for d in self.decks.values()]}

    def to_dict(self) -> dict:
        """存档。rng 状态里的元组递归转列表以兼容 JSON。"""
        def jsonable(x):
            if isinstance(x, tuple):
                return [jsonable(i) for i in x]
            if isinstance(x, list):
                return [jsonable(i) for i in x]
            return x
        return {"turn": self.turn, "over": self.over, "log": self.log,
                "uid_counter": self._uid_counter, "rng": jsonable(self.rng.getstate()),
                "cards": [asdict(c) for c in self.cards.values()],
                "verbs": [asdict(v) for v in self.verbs.values()],
                "situations": [asdict(s) for s in self.situations.values()],
                "decks": [asdict(d) for d in self.decks.values()],
                "queue": list(self._queue), "spawned_once": sorted(self._spawned_once)}

    @classmethod
    def from_dict(cls, content: ContentBase, data: dict) -> "Game":
        """读档。内容库需与存档时一致（内容版本不一致不保证兼容）。"""
        def tupleize(x):
            if isinstance(x, list):
                return tuple(tupleize(i) for i in x)
            return x
        g = cls.__new__(cls)
        g.content = content
        g.rng = random.Random()
        g.rng.setstate(tupleize(data["rng"]))
        g.turn, g.over, g.log = data["turn"], data["over"], data["log"]
        g._uid_counter = data["uid_counter"]
        g.cards = {c["uid"]: CardInstance(**c) for c in data["cards"]}
        g.verbs = {v["uid"]: VerbInstance(**v) for v in data["verbs"]}
        g.situations = {s["uid"]: Situation(**s) for s in data["situations"]}
        g.decks = {d["def_id"]: DeckInstance(**d) for d in data["decks"]}
        g._queue = list(data["queue"])
        g._spawned_once = set(data["spawned_once"])
        return g