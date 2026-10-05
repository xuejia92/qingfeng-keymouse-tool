# -*- coding: utf-8 -*-
"""「🧰 小工具」页：以宫格展示可独立运行的小程序 + 用户自定义的程序。

布局
----
整页就是一块可滚动的宫格（**没有页内标题和说明文字**：标签页名已经说明了
这是什么页，再加一行文字纯属占地方）。里面是网格卡片，每张卡片是一个
`QToolButton`（图标在上、名称在下），悬停提示写的是小程序的用途说明。

**列数按可用宽度自动算**（2026-10-04 用户要求）：窗口宽就多排几列、窄就少排几列，
不再写死三列。触发点是卡片容器的 `resizeEvent`（不是页面的 resizeEvent，
原因见 `GridHolder`）。列数没变时不碰任何控件，所以常规拖动窗口不会反复重排。

两类卡片
--------
- **内置小程序**：来自 `app.mini_apps` 注册表，点击 -> `mini_window.open_mini_app()`
  在独立窗口里运行（加小程序不用改本文件，见 `app/mini_apps/__init__.py`）。
- **自定义程序**（2026-10-04 用户要求）：用户自己挑的电脑里的程序/快捷方式，
  存在 `AppConfig.custom_tools`；点击 = 用系统默认方式打开它（等价双击），
  右键可重命名/删除。宫格末尾永远有一张「添加程序」卡片。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..config import CUSTOM_TOOLS_MAX
from ..mini_apps import MiniApp, all_apps
from ..tool_entries import display_name, file_icon, launch_program
from . import theme
from .mini_window import open_mini_app
from .widgets import emoji_icon

GRID_COLUMNS = 3            # 列数初值（真正排版时按可用宽度算，见 columns_for_width）
MAX_COLUMNS = 12            # 列数上限：再宽也不排成一条长龙
CARD_W, CARD_H = 136, 104   # 单张卡片尺寸（2026-10-04 用户要求"每个宫格宽高都小一点"）
CARD_ICON = 36              # 卡片里图标画多大
CARD_GAP = 8                # 卡片间距
PAGE_MARGIN = 16            # 页面留白
CUSTOM_NAME_MAX = 40        # 自定义条目显示名上限（与 config.clean_custom_tools 一致）
# 分区标题：**内置小程序与手动添加的程序分开显示**（2026-10-05 用户要求）
SECTION_BUILTIN = "内置小工具"
SECTION_CUSTOM = "我添加的程序"

log = logging.getLogger(__name__)


def _emoji_icon(emoji: str, size: int):
    """兼容旧调用点：emoji -> QIcon 的实现已提升到 widgets.emoji_icon。"""
    return emoji_icon(emoji, size)


class GridHolder(QWidget):
    """承载卡片的容器：宽度一变就把新宽度回调出去。

    为什么要这一层：滚动区是 `setWidgetResizable(True)`，容器的宽度由**视口**决定，
    页面（ToolsTab）resize 时它未必已经跟着变，在页面的 resizeEvent 里量宽度会慢一拍。
    装在容器自己的 resizeEvent 上才是"宽度真的变了"这个时机。
    """

    def __init__(self, on_width_changed, parent=None):
        super().__init__(parent)
        self._on_width_changed = on_width_changed

    def resizeEvent(self, event):       # noqa: N802（Qt 命名）
        super().resizeEvent(event)
        # 用事件里的新尺寸而不是 self.width()：两者在真实 resize 时相同，
        # 但显式取事件参数后，测试投一个 QResizeEvent 就能确定性地验证这条契约。
        self._on_width_changed(event.size().width())


def _grid_qss() -> str:
    """卡片配色（页面级内联样式，颜色全部走主题令牌 -> 切主题自动跟随）。

    ⚠️ QToolButton **不在**全局按钮基线（那里只有 QPushButton）里，所以这里
    必须把 border/背景/文字色写全，否则卡片会是 Windows 原生灰按钮的样子。
    """
    t = theme.token
    return f"""
    /* 网格容器透明：底色交给标签页自己的面板底（QTabWidget::pane），
       否则容器只在第一行卡片那一带铺出灰底、下方露出白色，看着像一条横幅 */
    QWidget#toolsGrid {{
        background: transparent;
    }}
    QWidget#toolsGrid QToolButton {{
        background: {t('card_bg')};
        color: {t('text')};
        border: 1px solid {t('border')};
        border-radius: 8px;
        padding: 8px 4px 6px 4px;
        font-size: 9pt;
    }}
    QWidget#toolsGrid QToolButton:hover {{
        border: 1px solid {t('primary')};
        background: {t('primary_soft')};
        color: {t('primary')};
    }}
    QWidget#toolsGrid QToolButton:pressed {{
        background: {t('pressed_bg')};
    }}
    """


class ToolsTab(QWidget):
    """小工具九宫格页（内置小程序 + 用户自定义的程序）。"""

    # 自定义程序增/删/改名（主窗口据此让中键菜单的九宫格管理页跟上：
    # 否则用户加完程序，九宫格的候选里要等重启才看得到）
    toolsChanged = Signal()

    def __init__(self, cfg=None, parent=None):
        super().__init__(parent)
        self.setObjectName("toolsPage")
        self._cfg = cfg
        # 自定义条目的**工作副本**（加/删/改名都在这里改，改完写回 cfg 并保存）
        self._custom: list[dict] = (
            [dict(item) for item in (cfg.custom_tools or [])]
            if cfg is not None else [])
        self._cards: list[QToolButton] = []
        # 分开存两份：内置小程序与用户手动添加的程序**分区显示**（2026-10-05 用户要求）
        self._builtin_cards: list[QToolButton] = []
        self._custom_cards: list[QToolButton] = []
        self._builtin_title: QLabel | None = None
        self._custom_title: QLabel | None = None
        self._columns = GRID_COLUMNS       # 当前列数（按宽度算出来，见 _apply_width）
        self._stretch_row: int | None = None
        self._add_card: QToolButton | None = None   # 末尾的「添加程序」卡片
        self._build_ui()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # 页面直接就是宫格：不放页内标题/说明（标签页名已经说明了这是什么页）
        root.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root.setSpacing(10)

        self.grid_holder = GridHolder(self._apply_width)
        self.grid_holder.setObjectName("toolsGrid")
        self.grid_holder.setStyleSheet(_grid_qss())
        # ⚠️ 横向必须是 Ignored：卡片是 setFixedSize 的，于是 QGridLayout 的
        # **最小宽度**= 当前列数 × 卡片宽 + 间距。而 setWidgetResizable 的滚动区
        # 不会把容器压到它自己的最小宽度以下 —— 结果是「列数锁死」：窗口拖窄了，
        # 容器宽度不跟着变小（实测宽 560 的页面里容器仍是 568），回调永远算不出更少的
        # 列，卡片直接被裁掉。设成 Ignored 后容器宽度完全跟随视口，列数才能减少。
        self.grid_holder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(CARD_GAP)
        # 末尾留一条弹性列吸收多余宽度：否则只有一两个小程序时，QGridLayout 会把
        # 空白平摊到「有内容的列」上，卡片会被推到页面中间（2026-10-03 离屏核验发现）。
        self.grid.setColumnStretch(self._columns, 1)
        self._empty_label = QLabel("还没有任何小工具\n点右下角的「添加程序」把电脑里的程序加进来")
        self._empty_label.setStyleSheet(
            f"color:{theme.token('text_muted')};font-size:10pt;padding:24px;")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self._empty_label, 0, 0)
        # 分区标题（内置 / 我添加的）：位置与显示与否都交给 _layout_cards 统一排
        self._builtin_title = self._make_section_title(SECTION_BUILTIN)
        self._custom_title = self._make_section_title(SECTION_CUSTOM)
        # 「添加程序」卡片：常驻自定义区末尾（不属于 _cards，reload 不重建它）
        self._add_card = self._make_add_card()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(self.grid_holder)
        # 滚动区与视口都要透明：它们默认按 palette 的 Base 角色铺白底，
        # 会把标签页的面板底色盖掉（和上面网格容器那条注释是同一个问题）
        area.setStyleSheet("background: transparent;")
        area.viewport().setStyleSheet("background: transparent;")
        root.addWidget(area, 1)

        self.reload()

    # ---------- 自适应列数 ----------
    @staticmethod
    def columns_for_width(width: int) -> int:
        """按可用宽度算列数（至少 1 列、至多 MAX_COLUMNS）。

        纯函数，便于直接测边界：宽度不够放一张卡片时也得给 1 列，
        否则卡片会被压扁或跑到可视区外。
        """
        usable = max(0, int(width))
        columns = (usable + CARD_GAP) // (CARD_W + CARD_GAP)
        return int(max(1, min(MAX_COLUMNS, columns)))

    def columns(self) -> int:
        """当前列数（测试与排版都以它为准）。"""
        return self._columns

    def _apply_width(self, width: int) -> None:
        """容器宽度变化：列数真的变了才重排（列数没变时一个控件都不动）。"""
        columns = self.columns_for_width(width)
        if columns == self._columns:
            return
        self._columns = columns
        self._layout_cards()

    def _cell_count(self) -> int:
        """宫格里的卡片总数：工具卡片 + 末尾那张「添加程序」。"""
        return len(self._cards) + (0 if self._add_card is None else 1)

    @staticmethod
    def _make_section_title(text: str) -> QLabel:
        """区段小标题（内置小工具 / 我添加的程序）。

        刻意做得很轻：小字 + 弱化色，起分区作用但不抢卡片的风头。
        字号跟着卡片走（卡片也是 9pt），两者在视觉上是一组。
        """
        label = QLabel(text)
        label.setStyleSheet(f"color:{theme.token('text_muted')};"
                            "font-size:9pt;padding:4px 0 0 2px;")
        return label

    def sections(self) -> list[tuple[str, list[QToolButton]]]:
        """宫格的分区：[(标题, 该区卡片)]。

        内置区只在内置小程序存在时出现（正常情况下总有几个）；**自定义区永远出现**——
        「添加程序」卡片就住在那一区，那是添加的唯一入口，不能因为"还没有自定义程序"
        就把整个区（含入口）藏起来。
        """
        out: list[tuple[str, list[QToolButton]]] = []
        if self._builtin_cards:
            out.append((SECTION_BUILTIN, list(self._builtin_cards)))
        out.append((SECTION_CUSTOM, list(self._custom_cards)))
        return out

    def _layout_cards(self) -> None:
        """按当前列数把**标题 + 卡片**重摆一遍（先 removeWidget 再 addWidget）。

        摆放规则：标题独占一行（跨整行），卡片按列依次填；一个区段结束后，
        下一个标题另起一行。这样内置与自定义两块天然分开，卡片又能在同一套列宽下对齐。
        """
        columns = self._columns
        placeholders: list[tuple[str, object]] = []
        for title, cards in self.sections():
            placeholders.append(("title", title))
            placeholders.extend(("card", card) for card in cards)
            if title == SECTION_CUSTOM and self._add_card is not None:
                placeholders.append(("card", self._add_card))

        row, col = 0, 0
        for kind, item in placeholders:
            if kind == "title":
                if col:                     # 标题必须另起一行
                    row += 1
                    col = 0
                label = (self._builtin_title if item == SECTION_BUILTIN
                         else self._custom_title)
                self.grid.removeWidget(label)
                self.grid.addWidget(label, row, 0, 1, columns + 1)
                label.show()
                row += 1
            else:
                self.grid.removeWidget(item)
                self.grid.addWidget(item, row, col)
                col += 1
                if col >= columns:
                    row += 1
                    col = 0
        if col:
            row += 1
        self._reset_stretch(row)

    def _reset_stretch(self, used_rows: int) -> None:
        """重设弹性行列：内容整齐贴左上角，多余宽度/高度都留在右下方。

        `used_rows` 是内容实际占用的行数（**含分区标题行**）—— 写错的话弹性行会
        落在内容中间，把最后一行卡片顶到页面中部。
        """
        for col in range(0, MAX_COLUMNS + 2):
            self.grid.setColumnStretch(col, 0)
        self.grid.setColumnStretch(self._columns, 1)
        if self._stretch_row is not None:
            self.grid.setRowStretch(self._stretch_row, 0)
        self._stretch_row = max(1, int(used_rows))
        self.grid.setRowStretch(self._stretch_row, 1)

    def reload(self) -> None:
        """按注册表 + 自定义列表重建全部卡片（自定义部分**以配置为准**）。"""
        for card in self._cards:
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._builtin_cards = []
        self._custom_cards = []
        if self._stretch_row is not None:       # 上一轮的弹性行先撤掉，免得串位
            self.grid.setRowStretch(self._stretch_row, 0)
            self._stretch_row = None

        if self._cfg is not None:
            # reload = 从配置刷新：外部改了 cfg.custom_tools 也能反映到页面上
            self._custom = [dict(item) for item in (self._cfg.custom_tools or [])]

        apps = all_apps()
        self._empty_label.setVisible(not apps and not self._custom)
        self._builtin_cards = [self._make_card(app) for app in apps]
        self._custom_cards = [self._make_custom_card(index, item)
                              for index, item in enumerate(self._custom)]
        # 分区标题只在有内容时显示（_layout_cards 里 show/hide）
        for label in (self._builtin_title, self._custom_title):
            if label is not None:
                label.hide()
        self._cards = self._builtin_cards + self._custom_cards

        # 卡片位置统一由 _layout_cards 按**当前可用宽度**决定，别再写死 3 列
        self._columns = self.columns_for_width(self.grid_holder.width())
        self._layout_cards()

    def cards(self) -> list[QToolButton]:
        """当前全部工具卡片（内置在前、自定义在后；不含「添加程序」），测试用。"""
        return list(self._cards)

    def _make_card(self, app: MiniApp) -> QToolButton:
        card = QToolButton()
        card.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        card.setIconSize(QSize(CARD_ICON, CARD_ICON))
        card.setIcon(_emoji_icon(app.icon, CARD_ICON))
        card.setText(app.name)
        card.setToolTip(f"{app.name}\n{app.desc}" if app.desc else app.name)
        card.setFixedSize(CARD_W, CARD_H)
        card.setCursor(Qt.PointingHandCursor)
        card.setAutoRaise(True)
        card.clicked.connect(lambda _=False, a=app: self.open_app(a))
        return card

    # ---------- 自定义程序 ----------
    def _make_custom_card(self, index: int, item: dict) -> QToolButton:
        card = QToolButton()
        card.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        card.setIconSize(QSize(CARD_ICON, CARD_ICON))
        card.setIcon(file_icon(item["path"], CARD_ICON))
        card.setText(item["name"])
        card.setToolTip(f"{item['name']}\n{item['path']}")
        card.setFixedSize(CARD_W, CARD_H)
        card.setCursor(Qt.PointingHandCursor)
        card.setAutoRaise(True)
        card.clicked.connect(lambda _=False, i=index: self.launch_custom(i))
        # 右键：打开 / 重命名 / 删除（内置小程序没有这些操作，不挂菜单）
        card.setContextMenuPolicy(Qt.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda _pos, i=index, w=card: self.show_custom_menu(i, w))
        return card

    def _make_add_card(self) -> QToolButton:
        """末尾那张「添加程序」卡片（虚线边框，和工具卡片区分开）。"""
        t = theme.token
        card = QToolButton()
        card.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        card.setIconSize(QSize(CARD_ICON, CARD_ICON))
        card.setIcon(_emoji_icon("➕", CARD_ICON))
        card.setText("添加程序")
        card.setToolTip("把电脑里的程序/快捷方式添加到小工具页")
        card.setFixedSize(CARD_W, CARD_H)
        card.setCursor(Qt.PointingHandCursor)
        card.setAutoRaise(True)
        card.setStyleSheet(
            "QToolButton { border: 1px dashed " + t("border") + ";"
            " background: transparent; color: " + t("text_muted") + "; }"
            "QToolButton:hover { border: 1px solid " + t("primary") + ";"
            " color: " + t("primary") + "; }")
        card.clicked.connect(lambda _=False: self.add_program())
        return card

    def add_program(self) -> None:
        """弹文件对话框挑一个程序，加成一张自定义卡片。"""
        path, _chosen = QFileDialog.getOpenFileName(
            self, "选择要添加的程序", "",
            "程序与快捷方式 (*.exe *.lnk *.bat *.cmd *.msi *.url);;所有文件 (*.*)")
        path = (path or "").strip()
        if not path:
            return
        if len(self._custom) >= CUSTOM_TOOLS_MAX:
            QMessageBox.information(
                self, "添加程序",
                f"自定义小工具最多 {CUSTOM_TOOLS_MAX} 个，先删掉一些再添加吧。")
            return
        name = display_name(path).strip()[:CUSTOM_NAME_MAX] or "程序"
        self._custom.append({"name": name, "path": path})
        self._persist()
        self.reload()
        self.toolsChanged.emit()

    def launch_custom(self, index: int) -> None:
        """打开一条自定义程序：等价于在资源管理器里双击它。"""
        if not (0 <= index < len(self._custom)):
            return
        launch_program(self._custom[index]["path"],
                       parent=self.window() or self)

    def show_custom_menu(self, index: int, card: QWidget) -> None:
        """自定义卡片的右键菜单：打开 / 重命名 / 删除。"""
        if not (0 <= index < len(self._custom)):
            return
        menu = QMenu(self)
        open_action = menu.addAction("打开")
        open_action.triggered.connect(lambda _=False, i=index: self.launch_custom(i))
        rename_action = menu.addAction("重命名")
        rename_action.triggered.connect(
            lambda _=False, i=index: self.rename_custom(i))
        menu.addSeparator()
        remove_action = menu.addAction("删除")
        remove_action.triggered.connect(
            lambda _=False, i=index: self.remove_custom(i))
        menu.exec(card.mapToGlobal(QPoint(card.width() // 2,
                                          card.height() // 2)))

    def rename_custom(self, index: int) -> None:
        if not (0 <= index < len(self._custom)):
            return
        item = self._custom[index]
        name, ok = QInputDialog.getText(self, "重命名小工具", "名称：",
                                        text=item["name"])
        if not ok:
            return
        name = name.strip()[:CUSTOM_NAME_MAX]
        if not name:
            return
        item["name"] = name
        self._persist()
        self.reload()
        self.toolsChanged.emit()

    def remove_custom(self, index: int) -> None:
        """移除一张自定义卡片（问一声；**不会删电脑上的程序本身**）。"""
        if not (0 <= index < len(self._custom)):
            return
        item = self._custom[index]
        answer = QMessageBox.question(
            self, "删除小工具",
            f"要从「小工具」页移除「{item['name']}」吗？\n"
            "（只是不再显示在这里，不会删除电脑上的程序本身）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        del self._custom[index]
        self._persist()
        self.reload()
        self.toolsChanged.emit()

    def _persist(self) -> None:
        """把自定义条目写回配置。save_flows=False：这操作与 flows/ 目录无关。"""
        if self._cfg is None:
            return
        self._cfg.custom_tools = [dict(item) for item in self._custom]
        try:
            self._cfg.save(save_flows=False)
        except Exception:               # noqa: BLE001（保存失败不该打断界面操作）
            log.exception("保存自定义小工具失败")

    # ---------- 打开 ----------
    def open_app(self, app: MiniApp) -> None:
        """在某张卡片被点击时调用：在独立窗口中打开该小程序。"""
        # anchor 只用来挑显示器，不作为父对象（见 mini_window 注释）
        open_mini_app(app, anchor=self.window())
