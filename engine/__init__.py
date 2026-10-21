"""密教式卡牌引擎——后端核心。

用法：
    from engine import ContentBase, Game

    content = ContentBase.load_file("example_content.json")
    game = Game(content, seed=42)
    # ……前端通过 game.snapshot() 渲染，通过 API 提交操作……
    game.end_turn()
"""
from .model import (AspectDef, CardDef, SlotDef, RecipeDef, VerbDef, DeckDef,
                    EndingDef, CardInstance, VerbInstance, Situation,
                    DeckInstance, ContentBase)
from .game import Game, GameError

__all__ = ["AspectDef", "CardDef", "SlotDef", "RecipeDef", "VerbDef", "DeckDef",
           "EndingDef", "CardInstance", "VerbInstance", "Situation",
           "DeckInstance", "ContentBase", "Game", "GameError"]