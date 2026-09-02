"""各标签页共用的参数控件与按钮着色辅助。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QWidget)

# 按钮变体 -> 内联样式（含 hover / pressed / disabled 全状态）
# 设计要点：直接给按钮 setStyleSheet（内联），不走全局 objectName + QSS。
# 这样按钮完全自包含，不会被 QSS 后代选择器级联污染——
# 之前 schedule_tab 在 QScrollArea 嵌套下，「QPushButton#btnDanger」规则的
# border / background 会跨级联泄漏到 viewport，导致右栏红染、文字消失。
_PRIMARY = (
    "QPushButton{background:#1668a8;color:white;border:1px solid #125a93;"
    "border-radius:4px;padding:4px 12px;font-size:10pt;}"
    "QPushButton:hover{background:#1d78c0;color:white;border-color:#1d78c0;}"
    "QPushButton:pressed{background:#125a93;color:white;border-color:#125a93;}"
    "QPushButton:disabled{background:#b9c2cb;color:#f0f3f6;border-color:#b9c2cb;}"
)
_SUCCESS = (
    "QPushButton{background:#2f9e5b;color:white;border:1px solid #278a4f;"
    "border-radius:4px;padding:4px 12px;font-size:10pt;}"
    "QPushButton:hover{background:#35b168;color:white;border-color:#35b168;}"
    "QPushButton:pressed{background:#278a4f;color:white;border-color:#278a4f;}"
    "QPushButton:disabled{background:#b9c2cb;color:#f0f3f6;border-color:#b9c2cb;}"
)
_DANGER = (
    "QPushButton{background:#d64541;color:white;border:1px solid #c0392b;"
    "border-radius:4px;padding:4px 12px;font-size:10pt;}"
    "QPushButton:hover{background:#e2544f;color:white;border-color:#e2544f;}"
    "QPushButton:pressed{background:#c0392b;color:white;border-color:#c0392b;}"
    "QPushButton:disabled{background:#b9c2cb;color:#f0f3f6;border-color:#b9c2cb;}"
)
_VARIANT_STYLES = {"primary": _PRIMARY, "success": _SUCCESS, "danger": _DANGER}


def set_variant(btn: QPushButton, variant: str) -> None:
    """给按钮设置颜色变体：primary=蓝(编辑/打开) success=绿(启动/运行)
    danger=红(停止/删除) neutral=默认灰白（继承主窗口 QSS）。

    通过内联样式表实现：内联样式优先级最高，外部 QSS 怎么级联都不会覆盖到本按钮，
    也避免 objectName 的 ID 选择器（#btnDanger 等）跨 widget 树级联污染其他控件。
    """
    btn.setStyleSheet(_VARIANT_STYLES.get(variant, ""))


class StopConditionGroup(QGroupBox):
    """通用停止条件：次数（0=无限）+ 持续时长（0=不限），任一满足即停。"""

    changed = Signal()

    def __init__(self, title: str = "停止条件（次数与时间任一满足即自动停止）", parent=None):
        super().__init__(title, parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 8)

        lay.addWidget(QLabel("执行次数"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 999_999_999)
        self.count_spin.setSpecialValueText("0 = 无限")
        self.count_spin.setMinimumWidth(110)
        lay.addWidget(self.count_spin)

        lay.addSpacing(16)
        lay.addWidget(QLabel("持续时长"))
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.0, 604800.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSingleStep(1.0)
        self.duration_spin.setSuffix(" 秒")
        self.duration_spin.setSpecialValueText("0 = 不限")
        self.duration_spin.setMinimumWidth(130)
        lay.addWidget(self.duration_spin)

        lay.addStretch(1)
        self.count_spin.valueChanged.connect(self.changed)
        self.duration_spin.valueChanged.connect(self.changed)

    def values(self) -> tuple[int, float]:
        return self.count_spin.value(), round(self.duration_spin.value(), 1)

    def set_values(self, count: int, duration: float) -> None:
        self.count_spin.setValue(int(count))
        self.duration_spin.setValue(float(duration))


class StatusLabel(QWidget):
    """带状态圆点的任务状态显示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._dot = QLabel("●")
        self._text = QLabel("已停止")
        lay.addWidget(self._dot)
        lay.addWidget(self._text, 1)
        self.set_stopped()

    def set_running(self, note: str = "") -> None:
        self._dot.setStyleSheet("color: #2ecc71;")
        self._text.setText(f"运行中  {note}")

    def set_stopped(self, reason: str = "") -> None:
        self._dot.setStyleSheet("color: #95a5a6;")
        text = "已停止" if not reason else f"已停止（{reason}）"
        self._text.setText(text)
