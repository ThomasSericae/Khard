"""前端配置：配色、尺寸、资源路径与字体加载。"""
import os
import pygame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")


def _first_existing(*candidates: str) -> str:
    """返回第一个存在的路径；都不存在则返回第一个（让缺文件报错信息指向它）。"""
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


# 背景贴图：assets/ 或项目根目录皆可，替换成自己的图即可
BG_PATH = _first_existing(os.path.join(ASSETS, "background.png"),
                          os.path.join(ROOT, "background.png"))
SAVE_PATH = os.path.join(ROOT, "save.json")

CELL = 64                       # 一个方格的世界边长（px）
CARD = 56                       # 卡牌边长（一格留边距）
ZOOM_MIN, ZOOM_MAX = 0.4, 2.5   # 相机缩放范围
LERP = 14.0                     # 位置平滑速率（越大越跟手）

# 深蓝-金-白主色
INK = (10, 14, 24)
PANEL = (16, 22, 38)
PANEL_EDGE = (198, 160, 74)
GOLD = (214, 178, 90)
WHITE = (232, 234, 240)
DIM = (140, 148, 165)
CARD_BODY = (34, 40, 58)
VERB_BODY = (20, 26, 42)
DANGER = (178, 84, 72)

# 性质小图标配色（按内容库中性质顺序循环取色）
ASPECT_PALETTE = [
    (214, 178, 90), (150, 178, 214), (178, 120, 200), (120, 190, 150),
    (200, 110, 100), (120, 170, 210), (190, 160, 120), (140, 200, 200),
    (210, 140, 170), (160, 190, 110),
]

_font_cache = {}
_FONT_PATH = None


def _find_cjk_font() -> str | None:
    """在常见路径里找一款中文字体文件。

    不用 pygame.font.match_font：它在部分 Windows 机器上扫描注册表时会崩
    （pygame 已知 bug）。直接按路径探测最稳。
    """
    global _FONT_PATH
    if _FONT_PATH is not None:
        return _FONT_PATH or None
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyhbd.ttc",    # 微软雅黑 Bold
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
        r"C:\Windows\Fonts\Deng.ttf",      # 等线
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            _FONT_PATH = path
            return path
    _FONT_PATH = ""
    return None


def font(size: int, bold: bool = False) -> pygame.font.Font:
    """按字号取字体（带缓存）。size 会被钳制到 8~64。"""
    size = max(8, min(64, int(size)))
    key = (size, bold)
    if key not in _font_cache:
        path = _find_cjk_font()
        f = pygame.font.Font(path, size) if path else pygame.font.Font(None, size)
        f.set_bold(bold)
        _font_cache[key] = f
    return _font_cache[key]


def wrap_text(text: str, f: pygame.font.Font, width: int) -> list[str]:
    """按像素宽度折行。中文按字符断，英文按词断不了就按字符断。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if f.size(cur + ch)[0] > width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines