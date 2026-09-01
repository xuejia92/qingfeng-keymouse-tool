"""主窗口底部内嵌日志面板：可展开/收缩，替代原先屏幕左下角的悬浮窗。

结构：折叠头（点击切换展开）+ 日志文本框。展开时文本区显示、
高度增加；收缩时只留折叠头。由 MainWindow 负责在展开/收缩时同步
调整整个窗口的高度。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

_MAX_BLOCKS = 400          # 最多保留的日志行数
_TEXT_HEIGHT = 180         # 日志文本区展开后的高度


class LogPanel(QWidget):
    """内嵌日志面板：折叠头 + 可显隐的日志文本区。

    折叠头右侧带「每次运行清空日志」复选框和「清空日志」按钮。
    """

    expandedChanged = Signal(bool)       # 展开状态变化（供主窗口调整窗口高度）
    clearOnRunChanged = Signal(bool)     # 「每次运行清空」勾选状态变化（供主窗口持久化）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False
        self._summary = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- 折叠头行：标题按钮 + 清空选项 ----
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(8)

        self._header = QPushButton()
        self._header.setCheckable(True)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setToolTip("点击展开/收缩运行日志")
        self._header.toggled.connect(self._on_toggle)
        self._header.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 5px 10px;
                border: 1px solid #d8dee4;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                background: #f2f5f8;
                color: #57606a;
                font-size: 9.5pt;
            }
            QPushButton:hover { background: #e8eef4; color: #1668a8; }
            QPushButton:checked { background: #e8f1fa; color: #1668a8; }
        """)
        head_row.addWidget(self._header, 1)

        self.clear_on_run_box = QCheckBox("每次运行清空日志")
        self.clear_on_run_box.setToolTip(
            "勾选后，每次启动流程（含单步执行）时自动清空全部运行日志")
        self.clear_on_run_box.setStyleSheet(
            "QCheckBox { font-size: 9pt; color: #57606a; }")
        self.clear_on_run_box.toggled.connect(self.clearOnRunChanged.emit)
        head_row.addWidget(self.clear_on_run_box)

        self.clear_btn = QPushButton("🗑 清空日志")
        self.clear_btn.setToolTip("清空全部运行日志")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                padding: 4px 10px;
                border: 1px solid #d8dee4;
                border-bottom: none;
                border-top-right-radius: 6px;
                background: #f2f5f8;
                color: #57606a;
                font-size: 9pt;
            }
            QPushButton:hover { background: #fbe9e7; color: #d4380d; }
        """)
        self.clear_btn.clicked.connect(self.clear)
        head_row.addWidget(self.clear_btn)

        lay.addLayout(head_row)

        # ---- 日志文本区 ----
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_BLOCKS)
        self._text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._text.setStyleSheet(
            "QPlainTextEdit { color: #24292f; background: #fbfcfd;"
            "border: 1px solid #d8dee4; border-top: none;"
            "padding: 4px 6px; font-size: 9pt; }")
        self._text.setFont(QFont("Consolas", 9))
        self._text.setFixedHeight(_TEXT_HEIGHT)
        self._text.setVisible(False)
        lay.addWidget(self._text)

        self._refresh_header()

    # ---------- 折叠头 ----------
    def _on_toggle(self, checked: bool) -> None:
        self._expanded = checked
        self._text.setVisible(checked)
        self._refresh_header()
        self.expandedChanged.emit(checked)

    def _refresh_header(self) -> None:
        arrow = "▾" if self._expanded else "▸"
        base = f"运行日志 {arrow}"
        self._header.setText(f"{base}　{self._summary}" if self._summary else base)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._header.setChecked(expanded)   # 触发 _on_toggle

    # ---------- 日志与摘要 ----------
    def append(self, text: str) -> None:
        self._text.appendPlainText(text)
        self._text.moveCursor(QTextCursor.End)

    def clear(self) -> None:
        """清空全部运行日志。"""
        self._text.clear()

    @property
    def clear_on_run(self) -> bool:
        """是否在每次运行新流程时自动清空日志。"""
        return self.clear_on_run_box.isChecked()

    @clear_on_run.setter
    def clear_on_run(self, value: bool) -> None:
        self.clear_on_run_box.setChecked(bool(value))

    def set_summary(self, text: str) -> None:
        """运行摘要显示在折叠头（如「鼠标连点 · 找图:登录」）。"""
        self._summary = text
        self._refresh_header()
