# -*- coding: utf-8 -*-
"""「按分组拖放」的通用件：把条目拖进分组（自动化流程页 / 定时任务页共用）。

为什么自己处理 drop、不用 Qt 自带的 InternalMove
------------------------------------------------
这两棵左栏树的内容**都是每次从配置整体重建**出来的（分组顺序、运行中标记、启用状态
都按配置算）。让 Qt 先挪 `QTreeWidgetItem` 再重建会打架：它改的父子关系/行序下一秒就被
丢弃，还容易把分组头顺序搞乱。这里只做一件事 —— 把"拖到了哪个分组"翻译成
**一次配置改动**，由页面自己改配置 + 整体重建。

约定
----
分组与条目在 `QTreeWidgetItem` 的 `Qt.UserRole` 里存成元组：
- 分组头：`("group", 分组名)`（空串 = 未分组）
- 条目：  `(entry_kind, 条目 id)`，`entry_kind` 由构造参数给（"flow" / "task"）

拖动规则
--------
- 只有**条目**能拖（分组头不带 `ItemIsDragEnabled`，`startDrag` 里再兜一道）；
- 落到**分组头** → 归入该分组，排在该组末尾；
- 落到**条目** → 归入该条目所在分组，并按落点插到它**前面/后面**；
- 落到空白 / 没在拖条目 → 忽略。

⚠️ `reorder_for_group` 判"顺序有没有变"**不能用 `==`**：`Flow` / `ScheduleTask` 都是
dataclass，`==` 按**字段值**比较，内容相同的两个不同对象会被判等（"没动"与"动了但看起来
一样"分不开）。所以一律比对象身份。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget

from .. import mouse_menu, physical_hotkeys


def same_object_order(a, b) -> bool:
    """两个列表是不是**同一批对象、同样的顺序**（比 `is`，不是值相等）。"""
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


def reorder_for_group(items, entry, group: str, before=None, group_of=None):
    """算出"把 `entry` 移进 `group`（插在 `before` 前面）"之后的新顺序。

    - `group_of`：取一个条目的分组名（默认读 `entry.group`，空串 = 未分组）；
    - 返回**新列表**；若结果与 `items` 完全一致（同一批、同顺序，且 `entry` 本来就在
      目标分组里）则返回 `None` —— 那是"白拖一下"，调用方不该重建列表（会打断选中）。
    - 调用方负责把 `entry.group` 改成 `group`（这里不碰对象字段，只算顺序）。
    """
    group_of = group_of or (lambda x: getattr(x, "group", "") or "")
    group = str(group or "")
    if before is entry:
        return None                 # 拖到自己身上：位置本来就没变
    rest = [x for x in items if x is not entry]
    if before is not None and any(x is before for x in rest):
        index = next(i for i, x in enumerate(rest) if x is before)   # 插到落点前面
    else:
        index = len(rest)                                            # 默认排到该组末尾
        for i, x in enumerate(rest):
            if str(group_of(x) or "") == group:
                index = i + 1
    index = max(0, min(index, len(rest)))
    new_order = rest[:index] + [entry] + rest[index:]
    if str(group_of(entry) or "") == group and same_object_order(new_order, items):
        return None
    return new_order


class GroupDropTree(QTreeWidget):
    """可按分组拖放的左栏树（见模块 docstring）。"""

    # (条目 id, 目标分组名, 插到它前面的条目 id —— 空串 = 排到该组末尾)
    entry_dropped = Signal(str, str, str)

    def __init__(self, entry_kind: str, parent=None):
        super().__init__(parent)
        self.entry_kind = str(entry_kind)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.DragDrop)
        self._dragging_id: str | None = None

    # ---------- 拖起 ----------
    def startDrag(self, actions):          # noqa: N802（Qt 命名）
        item = self.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if not data or data[0] != self.entry_kind:
            return                          # 分组头 / 空白：不给拖
        self._dragging_id = data[1]
        # ⚠️ 拖拽期间必须让两个全局钩子让位（本工程实测：装着钩子拖动时，
        # 一次拖动只推进 2~4 个输入事件、单次卡 1000ms 以上 —— 钩子的 Python 回调
        # 抢不到 GIL，整条输入通路被拖死）。鼠标钩子是"让输入通路顺畅"，
        # 键盘钩子是"防被 Windows 超时静默摘掉 → 热键永久失效"，两个都要。
        # （详见 flow_dialog.StepList.startDrag 的同款处理与 mouse_menu.pause 的说明）
        paused = mouse_menu.pause()
        physical_hotkeys.pause()
        try:
            super().startDrag(actions)
        finally:
            if paused:
                mouse_menu.unpause()
            physical_hotkeys.unpause()
        self._dragging_id = None

    # ---------- 放下 ----------
    def drop_target(self, pos) -> tuple[str, str, str] | None:
        """把一次放置翻译成 `(条目 id, 目标分组, 插到它前面的条目 id)`。

        不合法（没在拖条目、落在空白处）返回 None。抽成独立方法是为了能直接测：
        真跑一次 `QDrag` 在离屏环境里不可靠。
        """
        entry_id = self._dragging_id
        if not entry_id:
            return None
        item = self.itemAt(pos)
        if item is None:
            return None
        group, before = "", ""
        current = item
        while current is not None:
            data = current.data(0, Qt.UserRole)
            if data and data[0] == "group":
                group = data[1]
                break
            if data and data[0] == self.entry_kind:
                before = data[1]
            current = current.parent()
        # 落在条目上：按落点在条目上/下决定插到它前面还是后面
        if before and self.dropIndicatorPosition() == \
                QTreeWidget.DropIndicatorPosition.BelowItem:
            before = ""
        return entry_id, group, before

    def dropEvent(self, event):            # noqa: N802
        target = self.drop_target(event.position().toPoint())
        if target is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.entry_dropped.emit(*target)
