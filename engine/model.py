"""静态定义层与运行时实例层。

静态定义（*Def）从内容 JSON 载入，运行时只读——它们是「内容」，不是「状态」。
运行时实例（CardInstance / VerbInstance / Situation / DeckInstance）构成游戏状态，
全部由纯数据组成，可整体 JSON 序列化（即存档）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# ==================== 静态定义层 ====================

@dataclass(frozen=True)
class AspectDef:
    id: str
    label: str
    icon: str = ""
    description: str = ""
    hidden: bool = False  # 对玩家隐藏，用于内部标记


@dataclass(frozen=True)
class CardDef:
    id: str
    label: str
    icon: str = ""
    description: str = ""
    aspects: dict[str, int] = field(default_factory=dict)  # {性质id: 数量}
    lifetime: int | None = None     # 存在回合数；None = 永久
    decay_to: str | None = None     # 寿命归零后变成的 CardDef id；None = 直接消失（衰变链）
    unique: bool = False            # 全局唯一（如主角身份卡），唯一卡不参与堆叠
    stackable: bool = True


@dataclass(frozen=True)
class SlotDef:
    """子槽位定义，隶属于 RecipeDef，不是独立实体。"""
    id: str
    label: str
    required: dict[str, int] = field(default_factory=dict)   # 准入性质门槛
    forbidden: dict[str, int] = field(default_factory=dict)  # 排斥性质
    greedy: bool = False      # 是否自动从桌面吸取合法卡
    essential: bool = False   # 是否必须填入才能提交（用于即时决策）
    capacity: int = 1         # 容量，按卡牌实例计数


@dataclass(frozen=True)
class RecipeDef:
    id: str
    label: str
    verb: str                                       # 宿主 VerbDef id
    requirements: dict[str, int] = field(default_factory=dict)  # 触发前提，负值表排斥
    craftable: bool = True              # 是否可被玩家直接发起；False = 只能经 linked/alternatives 进入
    warmup: int = 0                                 # 消耗回合数；0 = 零回合即时配方
    slots: list[SlotDef] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)     # 结算效果原语列表，见 game._apply_effects
    linked: list[str] = field(default_factory=list)       # 显式移交列表（方案一）
    alternatives: list[dict] = field(default_factory=list)  # [{"recipe": id, "chance": 百分比}]
    start_text: str = ""
    end_text: str = ""


@dataclass(frozen=True)
class VerbDef:
    id: str
    label: str
    icon: str = ""
    default_recipe: str | None = None   # 默认事件（无卡可拖时槽自己引发的配方）
    auto: bool = False                  # 生成时自动开始默认配方（心跳用）
    spawn_condition: dict | None = None  # None = 开局即存在；见 game._spawn_verbs
    lifetime: int | None = None         # 槽自身存在回合数；None = 永久
    grid_w: int = 2                     # 占位尺寸，默认 2×2（四个方格）
    grid_h: int = 2


@dataclass(frozen=True)
class DeckDef:
    id: str
    cards: dict[str, int] = field(default_factory=dict)  # {CardDef id: 张数}，有限多重集合
    reset_on_empty: bool = True         # 抽空后是否重置洗牌


@dataclass(frozen=True)
class EndingDef:
    id: str
    label: str
    text: str = ""
    cg: str = ""                                     # 结局 CG 资源路径
    legacy_spawn: dict[str, int] = field(default_factory=dict)  # 下局开局注入的卡


# ==================== 运行时实例层 ====================

@dataclass
class CardInstance:
    uid: str
    def_id: str
    stack: int = 1                     # 堆叠数量
    remaining_life: int | None = None  # 剩余回合寿命（取定义的 lifetime，不覆写性质）
    # location 三态：
    #   {"kind": "table", "x": int, "y": int}        桌面网格
    #   {"kind": "verb",  "verb": uid}               行动槽主卡口（尚未开始）
    #   {"kind": "main",  "situation": uid}          局面主卡口（已关押）
    #   {"kind": "slot",  "situation": uid, "slot": slot_id}
    location: dict = field(default_factory=dict)


@dataclass
class VerbInstance:
    uid: str
    def_id: str
    x: int = 0
    y: int = 0
    remaining_life: int | None = None
    situation: str | None = None       # 正在运行的 Situation uid；None = 空闲


@dataclass
class Situation:
    """事件状态机实例：一个槽同一时刻只跑一个局面。"""
    uid: str
    verb: str                          # 宿主 VerbInstance uid
    recipe: str                        # 当前 RecipeDef id
    elapsed: int = 0                   # 已耗回合
    phase: str = "running"             # running / awaiting_input
    main: list[str] = field(default_factory=list)        # 主卡口关押的卡 uid
    slots: dict[str, list[str]] = field(default_factory=dict)  # slot_id -> [卡 uid]
    chain: list[str] = field(default_factory=list)       # 移交链轨迹（调试/存档用，机制上不读）


@dataclass
class DeckInstance:
    def_id: str
    queue: list[str] = field(default_factory=list)  # 洗牌后的剩余 CardDef id 队列


# ==================== 内容库 ====================

class ContentBase:
    """内容库：从 JSON 载入全部静态定义并做引用校验。

    支持两种载入方式：
    - load_file(path)：单个合并 JSON，键为 aspects/cards/verbs/recipes/decks/endings；
    - load_dir(path)：目录下 aspects.json、cards.json …… 每个文件一个数组。
    """

    def __init__(self, data: dict):
        self.start_spawn: dict[str, int] = data.get("start_spawn", {})  # 开局注入的卡
        self.aspects = {a["id"]: AspectDef(**a) for a in data.get("aspects", [])}
        self.cards = {c["id"]: CardDef(**c) for c in data.get("cards", [])}
        self.verbs = {v["id"]: VerbDef(**v) for v in data.get("verbs", [])}
        self.decks = {d["id"]: DeckDef(**d) for d in data.get("decks", [])}
        self.endings = {e["id"]: EndingDef(**e) for e in data.get("endings", [])}
        self.recipes = {}
        for r in data.get("recipes", []):
            r = dict(r)
            r["slots"] = [SlotDef(**s) for s in r.get("slots", [])]
            self.recipes[r["id"]] = RecipeDef(**r)
        self._validate()

    @classmethod
    def load_file(cls, path: str | Path) -> "ContentBase":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def load_dir(cls, path: str | Path) -> "ContentBase":
        path = Path(path)
        data = {}
        for key in ("aspects", "cards", "verbs", "recipes", "decks", "endings"):
            f = path / f"{key}.json"
            if f.exists():
                with open(f, encoding="utf-8") as fp:
                    data[key] = json.load(fp)
        return cls(data)

    def _validate(self):
        """启动时把所有悬空引用一次性报出来，而不是运行到一半才炸。"""
        errors = []
        # 条件键的合法命名空间：裸名 = 性质；"card:<id>" = 卡种
        cond_keys = set(self.aspects) | {f"card:{i}" for i in self.cards}

        for c in self.cards.values():
            for a in c.aspects:
                if a not in self.aspects:
                    errors.append(f"卡牌 {c.id} 引用了不存在的性质 {a}")
            if c.decay_to and c.decay_to not in self.cards:
                errors.append(f"卡牌 {c.id} 的衰变目标 {c.decay_to} 不存在")

        for v in self.verbs.values():
            if v.default_recipe and v.default_recipe not in self.recipes:
                errors.append(f"行动槽 {v.id} 的默认配方 {v.default_recipe} 不存在")

        for r in self.recipes.values():
            if r.verb not in self.verbs:
                errors.append(f"配方 {r.id} 的宿主槽 {r.verb} 不存在")
            for k in r.requirements:
                if k not in cond_keys:
                    errors.append(f"配方 {r.id} 的前提 {k} 既不是性质也不是卡种")
            for rid in r.linked:
                if rid not in self.recipes:
                    errors.append(f"配方 {r.id} 移交目标 {rid} 不存在")
            for alt in r.alternatives:
                if alt["recipe"] not in self.recipes:
                    errors.append(f"配方 {r.id} 的替代配方 {alt['recipe']} 不存在")
            for s in r.slots:
                for k in list(s.required) + list(s.forbidden):
                    if k not in cond_keys:
                        errors.append(f"配方 {r.id} 槽位 {s.id} 的条件 {k} 既不是性质也不是卡种")

        for d in self.decks.values():
            for cid in d.cards:
                if cid not in self.cards:
                    errors.append(f"卡组 {d.id} 引用了不存在的卡牌 {cid}")

        for e in self.endings.values():
            for cid in e.legacy_spawn:
                if cid not in self.cards:
                    errors.append(f"结局 {e.id} 的传承卡牌 {cid} 不存在")

        if errors:
            raise ValueError("内容包校验失败：\n" + "\n".join(f"  - {e}" for e in errors))