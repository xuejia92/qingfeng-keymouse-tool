"""各标签页共用的参数控件与按钮着色辅助。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QWidget)

# 按钮变体 -> objectName（配合主窗口全局 QSS 着色）
VARIANTS = {"primary": "btnPrimary", "success": "btnSuccess",
            "danger": "btnDanger", "neutral": ""}


def set_variant(btn: QPushButton, variant: str) -> None:
    """给按钮设置颜色变体：primary=蓝(编辑/打开) success=绿(启动/运行)
    danger=红(停止/删除) neutral=默认灰白。"""
    btn.setObjectName(VARIANTS.get(variant, ""))
    style = btn.style()
    style.unpolish(btn)
    style.polish(btn)


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
