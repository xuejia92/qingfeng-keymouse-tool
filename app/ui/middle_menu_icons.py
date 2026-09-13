"""中键菜单的预设图标：把「图标 key」映射成可用的 QIcon。

设计取舍：配置里只存**短的 ASCII key**（如 "rocket"），不存 emoji、更不存图片路径。
显示时再用系统 emoji 字体（Segoe UI Emoji）把 emoji 画成 QIcon。好处是
config.json 可移植、可读、可手改，换机器/换字体也不会失效；key 认不出来就当作无图标。
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QIcon, QPainter, QPixmap

ICON_SIZE = 16                      # 菜单/列表里图标统一 16px，和条目行高匹配
_EMOJI_FONT = "Segoe UI Emoji"

# (key, emoji, 中文名)；key 为空串 = 无图标（菜单里留空不显示）。
# 顺序 = 宫格里的排布顺序：常用的排前面，「无图标」恒在第一格。
# 注意：key 一旦发布就不能改/删（用户 config.json 里存的就是它），只能新增。
PRESET_ICONS: list[tuple[str, str, str]] = [
    ("", "", "无图标"),

    # 运行与流程
    ("play", "▶️", "运行"),
    ("pause", "⏸️", "暂停"),
    ("stop", "⏹️", "停止"),
    ("rocket", "🚀", "火箭"),
    ("bolt", "⚡", "快捷"),
    ("refresh", "🔄", "刷新"),
    ("recycle", "♻️", "循环"),

    # 常用标记
    ("star", "⭐", "星标"),
    ("fire", "🔥", "常用"),
    ("pin", "📌", "置顶"),
    ("flag", "🚩", "标记"),
    ("tag", "🏷️", "标签"),
    ("bookmark", "🔖", "书签"),
    ("check", "✅", "完成"),
    ("cross", "❌", "取消"),
    ("warning", "⚠️", "警告"),
    ("info", "ℹ️", "信息"),
    ("bulb", "💡", "点子"),

    # 文件与数据
    ("folder", "📁", "文件夹"),
    ("doc", "📄", "文档"),
    ("note", "📝", "记录"),
    ("sheet", "📊", "图表"),
    ("clip", "📋", "列表"),
    ("clip2", "📎", "附件"),
    ("save", "💾", "保存"),
    ("trash", "🗑️", "删除"),
    ("pencil", "✏️", "编辑"),
    ("cloud", "☁️", "云"),

    # 网页与网络
    ("globe", "🌐", "网页"),
    ("link", "🔗", "链接"),
    ("download", "⬇️", "下载"),
    ("upload", "⬆️", "上传"),
    ("wifi", "📶", "网络"),
    ("mail", "✉️", "邮件"),
    ("chat", "💬", "对话"),
    ("people", "👥", "用户"),

    # 屏幕与媒体
    ("image", "🖼️", "图片"),
    ("camera", "📷", "截图"),
    ("video", "🎬", "视频"),
    ("music", "🎵", "音乐"),
    ("mic", "🎤", "录音"),
    ("sound", "🔊", "音量"),

    # 时间与调度
    ("clock", "⏰", "时间"),
    ("calendar", "📅", "日历"),
    ("bell", "🔔", "提醒"),

    # 系统与开发
    ("gear", "⚙️", "设置"),
    ("tools", "🛠️", "工具"),
    ("wrench", "🔧", "维护"),
    ("puzzle", "🧩", "插件"),
    ("key", "🔑", "密钥"),
    ("lock", "🔒", "锁定"),
    ("shield", "🛡️", "防护"),
    ("keyboard", "⌨️", "键盘"),
    ("mouse", "🖱️", "鼠标"),
    ("code", "💻", "代码"),
    ("robot", "🤖", "机器人"),
    ("sparkles", "✨", "智能"),

    # 业务与查找
    ("search", "🔍", "查找"),
    ("money", "💰", "金额"),
    ("card", "💳", "支付"),
    ("target", "🎯", "目标"),
]

# 空 key 不参与映射：它代表「无图标」，对外一律以空串呈现（见下方 finder）。
# 注意 "无图标" 这个中文名仍保留在 PRESET_ICONS 里，供下拉框直接取用。
_EMOJI_BY_KEY = {k: e for k, e, _ in PRESET_ICONS if k}
_LABEL_BY_KEY = {k: n for k, _, n in PRESET_ICONS if k}
_cache: dict[str, QIcon] = {}


def emoji_for(key: str) -> str:
    """图标 key -> emoji；空/未知 key 返回空串（=无图标）。"""
    return _EMOJI_BY_KEY.get((key or "").strip(), "")


def label_for(key: str) -> str:
    """图标 key -> 中文名；空/未知 key 返回空串（=无图标）。"""
    return _LABEL_BY_KEY.get((key or "").strip(), "")


def _render(emoji: str) -> QIcon:
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        font = QFont(_EMOJI_FONT)
        font.setPixelSize(ICON_SIZE - 1)
        painter.setFont(font)
        painter.drawText(QRect(0, 0, ICON_SIZE, ICON_SIZE), Qt.AlignCenter, emoji)
    finally:
        painter.end()          # QPainter 必须显式结束，否则 pixmap 不完整
    return QIcon(pixmap)


def icon_for(key: str) -> QIcon:
    """图标 key -> QIcon（按 emoji 缓存）。无图标 / 未知 key 返回空 QIcon。"""
    emoji = emoji_for(key)
    if not emoji:
        return QIcon()
    if emoji not in _cache:
        _cache[emoji] = _render(emoji)
    return _cache[emoji]


def blank_icon() -> QIcon:
    """等尺寸的全透明图标。

    菜单里若「有的条目有图标、有的没有」，Qt 只给有图标的那几条让出图标列，
    没有的条目文字会顶到最左，两行文字左边缘对不齐。给无图标的条目补一个
    透明占位图标即可对齐（视觉上仍然是「留空」）。

    注意：这里直接给一张空 pixmap，**不要**用画空格字符的办法——空格在某些
    字体回退下会画出可见的豆腐块。
    """
    if "__blank__" not in _cache:
        pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
        pixmap.fill(Qt.transparent)
        _cache["__blank__"] = QIcon(pixmap)
    return _cache["__blank__"]
