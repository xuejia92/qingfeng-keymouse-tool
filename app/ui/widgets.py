"""各标签页共用的参数控件与按钮着色辅助。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QWidget)

from . import theme

# ---------------------------------------------------------------------------
# 表单排版节奏（设置页 / 各编辑弹窗共用，2026-10-01）
# ---------------------------------------------------------------------------
# 为什么要有这一组常量：QFormLayout 的默认 horizontalSpacing/verticalSpacing 只有
# **6px**，中文标签一长就紧贴输入框；而且各页面各写一套间距时，疏密会不一致
#（设置页 16/10、步骤编辑弹窗 6/6）。统一到这里，改一处所有表单一起变。
FORM_MARGINS = (12, 12, 12, 12)     # 表单内容与分组框/对话框边框的距离
FORM_H_SPACING = 15                 # 标签列 <-> 控件列
FORM_V_SPACING = 9                  # 行与行


def polish_form(form: QFormLayout) -> QFormLayout:
    """统一 QFormLayout 的留白与标签对齐，返回 form 便于链式书写。

    标签**右对齐**是刻意的：这样「标签 -> 输入框」的间隙处处一致
    （左对齐时短标签留一大片空白、长标签又只剩 6px，疏密不均）。
    """
    form.setContentsMargins(*FORM_MARGINS)
    form.setHorizontalSpacing(FORM_H_SPACING)
    form.setVerticalSpacing(FORM_V_SPACING)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return form

# ---------------------------------------------------------------------------
# 展开/收起指示符（2026-10-01）
# ---------------------------------------------------------------------------
# ⚠️ **别再用 `▾`/`▸`（U+25BE/U+25B8）**：这两个字在 Microsoft YaHei 的**粗体**字面里没有，
# 而分组头/日志条正好是 `font-weight: 700` + 9pt——实测渲染成一个方框（缺字），
# 真机上能不能看见三角全看系统字体回退给不给面子。
# `▼`(U+25BC) / `▲`(U+25B2) 在雅黑的所有字重下都有字形，大小和文字也协调。
DISCLOSURE_EXPANDED = "▼"     # 已展开（点一下收起）
DISCLOSURE_COLLAPSED = "▲"    # 已收起（点一下展开）


def set_variant(btn: QPushButton, variant: str, font_pt: float | None = None) -> None:
    """给按钮设置颜色变体：primary=蓝(编辑/打开) success=绿(启动/运行)
    danger=红(停止/删除) neutral=默认灰白（继承主窗口 QSS）。

    样式由主题引擎按当前主题生成（颜色令牌化），所以**切换主题后按钮会自动换色**；
    仍然是内联样式：优先级最高，外部 QSS 怎么级联都不会覆盖到本按钮，
    也避免 objectName 的 ID 选择器（#btnDanger 等）跨 widget 树级联污染其他控件。

    `font_pt`：基准字号（默认跟随应用默认 10pt）。**控件自带样式表时，页面级 QSS
    的字号规则对它不生效**——所以整页压到 9pt 的页面（设置页）要向这里显式传字号。
    """
    btn.setStyleSheet(theme.build_variant_qss(variant, font_pt=font_pt))


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
