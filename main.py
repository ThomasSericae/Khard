"""入口：场景管理 + 主循环。

用法（在项目根目录下）：
    python main.py                 # 自动查找示例内容包
    python main.py my_content.json # 用你自己的内容包

注意：请运行 main.py，不要直接运行 frontend/ 里的文件。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, os.path.join(ROOT, "frontend")):
    if p not in sys.path:
        sys.path.insert(0, p)  # 兼容 IDE 把 frontend 当 sources root 的摆法

import pygame

try:
    from frontend.scenes import MenuScene, GameScene
except ImportError:
    from frontend.scenes import MenuScene, GameScene


def _find_content(name: str) -> str:
    """在根目录、engine/、assets/ 里依次找内容包。"""
    for p in (name, os.path.join(ROOT, name),
              os.path.join(ROOT, "engine", name),
              os.path.join(ROOT, "assets", name)):
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"找不到内容包 {name}（找过根目录、engine/、assets/）")


CONTENT_PATH = _find_content(sys.argv[1] if len(sys.argv) > 1 else "example_content.json")


class App:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("密教引擎")
        self.screen = pygame.display.set_mode((1280, 800), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.running = True
        self.scene = MenuScene(self)

    # ---- 场景切换 ----

    def start_game(self, load_save: bool = False, legacy_spawn: dict | None = None):
        self.scene = GameScene(self, CONTENT_PATH,
                               load_save=load_save, legacy_spawn=legacy_spawn)

    def to_menu(self):
        self.scene = MenuScene(self)

    def quit(self):
        self.running = False

    # ---- 主循环 ----

    def run(self):
        while self.running:
            dt = self.clock.tick(60) / 1000.0
            events = pygame.event.get()
            for ev in events:
                if ev.type == pygame.QUIT:
                    self.running = False
            self.scene.handle(events)
            self.scene.update(dt)
            self.scene.draw(self.screen)
            pygame.display.flip()
        pygame.quit()


if __name__ == "__main__":
    App().run()