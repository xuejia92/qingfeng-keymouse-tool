"""主窗口：装配各标签页、后台任务与全局热键调度。

窗口正常显示在任务栏；点 X 隐藏到托盘，退出走托盘菜单。
"""
from __future__ import annotations

import logging
import os
import sys
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QStatusBar, QTabWidget,
                               QToolButton, QToolTip, QWidget)

from ..config import APP_NAME, AppConfig, ClickerConfig, PresserConfig
from .. import cancel_key, hotkey_policy
from ..hotkey_manager import HotkeyManager
from ..keymap import hotkey_display
from ..mouse_menu import MouseMenuWatcher
from ..tasks import ClickTask, PressTask
from ..updater import compare_versions, install_update
from .clicker_tab import ClickerTab
from .finder_tab import FinderTab
from .flow_tab import FlowTab
from .frameless_window import FramelessMainWindow
from .middle_menu_tab import MiddleMenuTab, build_menu
from .presser_tab import PresserTab
from .quick_access_tab import QuickAccessTab
from .schedule_tab import ScheduleTab
from .settings_tab import SettingsTab
from .tools_tab import ToolsTab
from .update_dialog import AutoDownloader, VersionFetcher

# 基准设计分辨率与对应窗口尺寸：2560x1440 屏 → 1620x1030
# （2026-09-04 由 1300x900 加大到 1400x960；2026-10-03 用户要求「默认打开尺寸稍微大一点」
#  加到 1500x1030；2026-10-05 用户要求「客户端宽度稍微大一点」→ 宽度单加 1500→1620。
#  改基准而不是改缩放比例，可以让**所有分辨率**都同比例变大：1920x1080 由
#  1125x772 → 1215x772；而小屏（≤1366 宽）本来就走 _MIN_WINDOW 下限，不受影响。
#  只加宽不加高：小工具页的宫格列数按宽度算，宽一点能多排一列。）
_BASE_SCREEN = (2560, 1440)
_BASE_WINDOW = (1620, 1030)
# 窗口尺寸下限（防止屏幕太小时缩到没法用）
_MIN_WINDOW = (980, 660)
# 中键在"菜单外面按下"关掉菜单后，这段时间内的中键**抬起**不再触发重开。
# 一次中键 = 按下 + 抬起两个事件，按下已经把它当"开关"用掉了（见
# _on_menu_button_pressed 的中键分支）；不吞掉抬起就会"关掉又立刻重开"。
# 用**时间戳**而不是布尔标志：万一抬起事件丢了，也只会吞掉这一瞬的中键，
# 不会把之后真正想唤菜单的那次点击也吃掉。
MENU_SWALLOW_TRIGGER_SEC = 0.8


def auto_window_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """按显示器分辨率动态计算主窗口尺寸。

    以 2560x1440 屏对应 1500x1030 为基准，按宽高各自比例取较小的缩放系数
    （保证窗口完整落在屏幕内）：
    - 分辨率 >= 基准（如 4K/2K）：保持 1500x1030，不放大
    - 分辨率 < 基准：等比缩小，但宽高都不小于最小窗口尺寸
    """
    if screen_w <= 0 or screen_h <= 0:
        return _BASE_WINDOW
    scale = min(screen_w / _BASE_SCREEN[0], screen_h / _BASE_SCREEN[1])
    w = max(int(_BASE_WINDOW[0] * scale), _MIN_WINDOW[0])
    h = max(int(_BASE_WINDOW[1] * scale), _MIN_WINDOW[1])
    # 大屏不放大：封顶到设计尺寸（1080p 以上保持 1500x1030）
    w = min(w, _BASE_WINDOW[0])
    h = min(h, _BASE_WINDOW[1])
    return w, h


class _RunIndicator(QPushButton):
    """状态栏「运行任务」指示器：悬停立即弹出当前运行任务列表。

    无任务时隐藏；单个任务直接显示任务名，多个任务显示数量；
    鼠标悬停在按钮下方弹出任务列表（QToolTip 多行），点击切到自动化流程页。
    """

    def enterEvent(self, ev):
        super().enterEvent(ev)
        if self.isVisible() and self.text():
            QToolTip.showText(self.mapToGlobal(QPoint(0, self.height() + 4)),
                              self.toolTip(), self)


# 「设置」角标（标签栏最右侧的齿轮图标）的尺寸节奏（2026-10-03）
CORNER_ICON = 18            # 齿轮图标尺寸
CORNER_BTN = 28             # 按钮边长（正方形，才像"图标按钮"）
CORNER_RADIUS = 7           # 悬停/选中底色的圆角
CORNER_RIGHT_PAD = 12       # 按钮右边距：贴着窗口边缘会很局促（用户反馈"位置太丑"）


def gear_icon(size: int = CORNER_ICON, color: str = "") -> QIcon:
    """自绘的齿轮图标（**不依赖任何 emoji / 图标字体**）。

    为什么不用字体图标：这是**常驻**的窗口元素，一旦系统缺那个字形就会变成一个空白按钮
    （`frameless_window.CaptionButton` 里的窗口按钮就是因为这个才自绘的）。
    颜色默认取当前主题的文字色，所以深色主题下也是亮的——主题切换时重生成一次即可
    （见 `MainWindow._apply_window_theme`）。
    """
    from . import theme
    color = color or theme.token("text")
    scale = 4                       # 4x 超采样：小尺寸下边缘才干净
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        center = size / 2.0
        r_body = size * 0.335       # 轮体半径
        r_hole = size * 0.135       # 中心孔
        r_tip = size * 0.47         # 齿顶半径
        teeth, tooth_w = 8, size * 0.125
        for i in range(teeth):
            painter.save()
            painter.translate(center, center)
            painter.rotate(i * 360.0 / teeth)
            painter.drawRoundedRect(
                QRectF(-tooth_w / 2, -r_tip, tooth_w, r_tip - r_body + 1.0),
                size * 0.02, size * 0.02)
            painter.restore()
        painter.drawEllipse(QPointF(center, center), r_body, r_body)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.drawEllipse(QPointF(center, center), r_hole, r_hole)
    finally:
        painter.end()
    return QIcon(pixmap)


def add_settings_corner(tabs: QTabWidget, settings_page: QWidget) -> QToolButton:
    """把「设置」做成标签栏**最右侧**的齿轮图标入口，返回那个按钮。

    为什么用 cornerWidget 而不是普通页签：`QTabBar` 的页签**只能从左往右排**，
    多余空间一律留在右边，没有"把某个页签右对齐"的接口。而「设置」是配置入口、
    不是日常操作页，放最右侧 + 只留图标更符合直觉。做法是：页签**照常加进
    QTabWidget**（这样 `setCurrentWidget`、页面信号接线、`settings_tab` 属性
    全都不受影响），只把它的页签按钮隐藏掉，改用这个角标按钮当入口。

    ⚠️ 角标按钮**不能直接塞给 setCornerWidget**：cornerWidget 是**紧贴标签栏右端**
    摆放的，直接放会顶到窗口右边缘、连悬停底色都被边界切一刀（2026-10-03 用户反馈
    「位置太丑」）。这里套一层容器专门留出右侧呼吸位，并把按钮在垂直方向居中，
    与页签的 `margin: 5px` 节奏对齐。
    """
    button = QToolButton()
    button.setObjectName("settingsCorner")
    button.setAutoRaise(True)
    button.setCursor(Qt.PointingHandCursor)
    button.setIconSize(QSize(CORNER_ICON, CORNER_ICON))
    button.setFixedSize(CORNER_BTN, CORNER_BTN)     # 正方形，才像"图标按钮"
    button.setFocusPolicy(Qt.NoFocus)               # 别抢 Tab 焦点
    button.setToolTip("设置")

    holder = QWidget()
    holder.setObjectName("settingsCornerHolder")
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, CORNER_RIGHT_PAD, 0)
    lay.setSpacing(0)
    lay.addWidget(button, 0, Qt.AlignVCenter)
    tabs.setCornerWidget(holder, Qt.TopRightCorner)

    button.clicked.connect(lambda: tabs.setCurrentWidget(settings_page))
    sync_settings_corner(button, tabs, settings_page)   # 初始外观
    return button


def sync_settings_corner(button: QToolButton, tabs: QTabWidget,
                         settings_page: QWidget) -> None:
    """按「当前是不是停在设置页」刷新齿轮的外观（图标色 + 悬停/选中底色）。

    页签按钮被隐藏后，切到设置页时标签栏不会高亮任何一项——不给反馈的话
    用户不知道自己"在设置里"。颜色走主题令牌，切主题由 hook 自动重映射；
    但**图标像素不吃 QSS 的颜色重映射**，所以这里连图标一起重画。
    """
    from . import theme
    tk = theme.token
    active = tabs.currentWidget() is settings_page
    # 常态用次要文字色：齿轮是"工具"而不是内容，压低一档才不与页签抢视线
    button.setIcon(gear_icon(CORNER_ICON, tk("primary") if active
                             else tk("text_dim")))
    background = tk("primary_soft") if active else "transparent"
    button.setStyleSheet(
        f"QToolButton{{background:{background};border:none;"
        f"border-radius:{CORNER_RADIUS}px;}}"
        f"QToolButton:hover{{background:{tk('hover_bg')};}}")


class MainWindow(FramelessMainWindow):
    hideToTrayNotice = Signal()
    # 从输入钩子线程请求关闭中键菜单（跑在那边的 Esc 监听不能直接碰 QWidget）
    _menuDismissRequested = Signal()

    def __init__(self, cfg: AppConfig, manager: HotkeyManager):
        super().__init__()
        self.cfg = cfg
        self.manager = manager
        # 注入实时配置供各热键录入控件做冲突校验
        hotkey_policy.set_config(cfg)
        # 界面主题与全局字体百分比：必须在任何控件构建之前应用
        #（之后构建的控件才会被主题映射；字号也要先设定，基线/内联样式生成时即按新字号）
        from . import theme
        theme.set_font_scale(getattr(cfg, "ui_font_scale", theme.UI_FONT_SCALE_DEFAULT))
        theme.apply_theme(getattr(cfg, "ui_theme", "light"))
        # 窗口样式：无边框 + 四角圆角卡片 + 自绘标题栏（标题栏随主题换色，见 frameless_window）
        # 点 X 隐藏到托盘，退出走托盘菜单（见 closeEvent）
        # 版本号 2026-10-01 曾挪到状态栏最右侧，2026-10-02 用户要求彻底去掉（界面上不再显示）
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(*_MIN_WINDOW)    # 无边框窗口没有系统下限，自己兜住
        self.resize(*self._window_size())

        self.click_task = ClickTask()
        self.press_task = PressTask()
        self.click_task.get_config = lambda: self._click_snapshot
        self.press_task.get_config = lambda: self._press_snapshot
        self._click_snapshot: ClickerConfig = cfg.clicker
        self._press_snapshot: PresserConfig = cfg.presser
        self._dispatch: dict[str, object] = {}

        tabs = QTabWidget()
        self.tabs = tabs
        self.clicker_tab = ClickerTab(cfg.clicker)
        self.presser_tab = PresserTab(cfg.presser)
        self.finder_tab = FinderTab(cfg.find_tasks)
        self.flow_tab = FlowTab(cfg)
        self.schedule_tab = ScheduleTab(cfg, self.flow_tab)
        self.middle_menu_tab = MiddleMenuTab(cfg)
        self.tools_tab = ToolsTab(cfg)
        self.settings_tab = SettingsTab(cfg)
        # 中键菜单 / 鼠标连点 / 键盘连按 / 找图点击 合成「⚡ 快捷操作」一页
        # （左侧导航 + 右侧内容）；四个控件对象与信号接线原样复用，只是不再各自占一个标签。
        # ⚠️ 导航项**不带 emoji**：🖱/⌨/🖼 在按钮文本里实测渲染成缺字方块（只有 📋 正常），
        # 设置页左侧导航也是纯文字，两页保持一致。
        self.quick_tab = QuickAccessTab([
            ("中键菜单", self.middle_menu_tab),
            ("鼠标连点", self.clicker_tab),
            ("键盘连按", self.presser_tab),
            ("找图点击", self.finder_tab),
        ])
        # 自动化流程是主功能，放第一个
        tabs.addTab(self.flow_tab, "🚀 自动化流程")
        tabs.addTab(self.schedule_tab, "⏰ 定时任务")
        tabs.addTab(self.quick_tab, "⚡ 快捷操作")
        # 「小工具」是日常使用页里最靠右的一个
        tabs.addTab(self.tools_tab, "🧰 小工具")
        # 「设置」是配置入口：页签加在最后但**隐藏按钮**，改用标签栏最右侧的齿轮图标
        # 进入（见 add_settings_corner 的说明）。页签按钮反正不显示，就不给它设图标/文字了。
        self.settings_tab_index = tabs.addTab(self.settings_tab, "")
        tabs.tabBar().setTabVisible(self.settings_tab_index, False)
        self.settings_btn = add_settings_corner(tabs, self.settings_tab)
        tabs.currentChanged.connect(
            lambda *_: sync_settings_corner(self.settings_btn, tabs,
                                            self.settings_tab))
        sync_settings_corner(self.settings_btn, tabs, self.settings_tab)

        # centralWidget = 标签页 + 底部可折叠日志面板 + 状态栏（都在圆角卡片的内容区里）
        from .log_panel import LogPanel
        self.log_panel = LogPanel()
        body = self.body_layout()
        body.addWidget(tabs, 1)
        body.addWidget(self.log_panel)
        body.addWidget(self.status_bar())    # 最后加：底部圆角归它
        self.log_panel.expandedChanged.connect(self._on_log_expanded)
        self._apply_window_theme()       # 按钮 + 顶部导航栏（主题令牌生成）
        # 主题切换时重刷窗口级样式（控件级内联样式由主题引擎自动重放）
        from . import theme
        theme.register_listener(self._apply_window_theme)
        # 接受外部文件拖放（拖入 .json 直接导入为自动化流程）
        self.setAcceptDrops(True)
        # 「显示/隐藏」置顶链的状态：_force_foreground 置为 True，失焦/隐藏时清除
        # （见 _force_foreground 注释——TOPMOST 不能定时取消，否则窗口会掉回被遮挡处）
        self._pinned_topmost = False

        bottom = QWidget()
        bottom.setObjectName("statusBarBody")
        # 状态栏统一小字号（9pt）+ 内边距压到最小 → 整条更矮（2026-10-01 用户要求）。
        # ⚠️ 这条只是兜底：**自带样式表的子控件不会继承父级的 QSS font-size**
        #（实测 status_hint 仍是应用字体 10pt），所以每个子控件
        # 都得自己显式写 font-size: 9pt。
        bottom.setStyleSheet(
            "QWidget#statusBarBody { font-size: 9pt; }")
        blay = QHBoxLayout(bottom)
        blay.setContentsMargins(8, 1, 8, 2)
        self.status_hint = QLabel()
        # 字号要在每个子控件上显式写：子控件自带样式表时不会继承父级的 QSS font-size
        self.status_hint.setStyleSheet("color: #888; font-size: 9pt;")
        # 运行任务指示器：显示当前正在运行的任务（单任务显示名称，多任务显示数量），
        # 悬停弹出任务列表；无任务时隐藏；点击切到自动化流程页查看。
        self.run_ind = _RunIndicator()
        self.run_ind.setCursor(Qt.PointingHandCursor)
        self.run_ind.setToolTip("")
        self.run_ind.setStyleSheet(
            "QPushButton{color:#2f9e5b; font-weight:600; border:none;"
            " background:transparent; padding:1px 8px; font-size:9pt;}"
            "QPushButton:hover{background:#e3f2ea; border-radius:4px;}")
        self.run_ind.clicked.connect(lambda: self.tabs.setCurrentWidget(self.flow_tab))
        self.run_ind.hide()
        stop_btn = QPushButton("全部停止")
        stop_btn.clicked.connect(self.stop_all)
        from .widgets import set_variant
        set_variant(stop_btn, "danger")
        # 状态栏里的小按钮：字号跟状态栏走、内边距收紧，别把它撑高
        stop_btn.setStyleSheet(
            stop_btn.styleSheet()
            + "\nQPushButton{font-size:9pt; padding:1px 10px;}")
        blay.addWidget(self.status_hint, 1)
        blay.addWidget(self.run_ind)
        blay.addWidget(stop_btn)
        self.statusBar().addPermanentWidget(bottom)
        self.statusBar().setStyleSheet("QStatusBar{border-top: 1px solid #ddd;}")
        # 状态栏左下角：红点 + 提示文字 + 更新按钮
        # 手动更新：检测到新版本仅提示，点「下载更新」才开始下载，完成后「重启升级」
        self.update_dot = QLabel()
        self.update_dot.setFixedSize(10, 10)
        self.update_dot.setStyleSheet("background: #e53935; border-radius: 5px;")
        self.update_dot.hide()
        self.statusBar().addWidget(self.update_dot)
        self.update_hint = QLabel()
        self.update_hint.setStyleSheet(
            "color: #1668a8; font-weight: 600; padding: 1px 6px; font-size: 9pt;")
        self.update_hint.hide()
        self.statusBar().addWidget(self.update_hint)
        self.update_btn = QPushButton("重启升级")
        self.update_btn.setStyleSheet(
            "QPushButton{background:#1668a8; color:white; border:none;"
            " border-radius:4px; padding:1px 10px; font-weight:600; font-size:9pt;}"
            "QPushButton:disabled{background:#9bb8d4;}")
        self.update_btn.clicked.connect(self._restart_upgrade)
        self.update_btn.hide()
        self.statusBar().addWidget(self.update_btn)
        # 下载进度条：下载中点击「下载中…」按钮可展开/收起
        self.update_progress = QProgressBar()
        self.update_progress.setFixedWidth(200)
        self.update_progress.setFixedHeight(16)
        self.update_progress.setTextVisible(True)
        self.update_progress.setStyleSheet(
            "QProgressBar{background:#eee; border:none; border-radius:3px;"
            " text-align:center; font-size:10px; color:#555;}"
            "QProgressBar::chunk{background:#1668a8; border-radius:3px;}")
        self.update_progress.hide()
        self.statusBar().addWidget(self.update_progress)
        self._progress_visible = True    # 下载中默认展开进度条，点击可收起
        self._refresh_status_hint()
        self._pending_update: tuple[str, list[str]] | None = None
        self._downloaded_file: str | None = None
        self._update_state = "idle"        # idle / available / downloading / ready / failed
        self._update_fail_reason = ""

        # ---- 信号接线 ----
        self.clicker_tab.changed.connect(self._on_clicker_changed)
        self.clicker_tab.toggleRequested.connect(self.toggle_clicker)
        self.clicker_tab.captureAboutToStart.connect(self._hide_for_capture)
        self.clicker_tab.captureFinished.connect(self._restore_after_capture)
        self.click_task.stateChanged.connect(self._on_click_state)
        self.click_task.progress.connect(self.clicker_tab.set_progress)

        self.presser_tab.changed.connect(self._on_presser_changed)
        self.presser_tab.toggleRequested.connect(self.toggle_presser)
        self.press_task.stateChanged.connect(self._on_press_state)
        self.press_task.progress.connect(self.presser_tab.set_progress)

        self.finder_tab.changed.connect(self._on_finder_changed)
        self.finder_tab.captureAboutToStart.connect(self._hide_for_capture)
        self.finder_tab.captureFinished.connect(self._restore_after_capture)

        self.flow_tab.changed.connect(self._on_flow_changed)
        self.flow_tab.flowStarted.connect(self._on_flow_started)
        # 任一流程运行状态变化 → 刷新左上角「运行中流程」红色浮层
        self.flow_tab.runningStateChanged.connect(self._refresh_running_overlay)
        # 拖动步骤排序只改了步骤，热键/流程名/其它页都无关：仅防抖落盘。
        # 走 changed 会在每次拖放后重注册全部全局热键并重建两个无关页面的列表。
        self.flow_tab.stepsChanged.connect(self._on_flow_steps_changed)
        # 流程增删改后，同步刷新定时任务页与中键菜单页的流程名兜底显示
        self.flow_tab.changed.connect(self.schedule_tab.on_flows_changed)
        self.flow_tab.changed.connect(self.middle_menu_tab.on_flows_changed)
        # 「🧰 小工具」页加/删/改了自定义程序 → 中键菜单九宫格的候选要立刻跟上
        # （否则用户加完程序，得重启才能放进九宫格）
        self.tools_tab.toolsChanged.connect(self.middle_menu_tab.on_tools_changed)
        # 中键菜单配置（菜单项 / 开关）变化：重配置监听并持久化
        self.middle_menu_tab.changed.connect(self._on_middle_menu_changed)
        # 「每次运行清空日志」勾选状态持久化到配置
        self.log_panel.clearOnRunChanged.connect(self._on_clear_log_setting)
        # 「只显示打印输出」勾选状态持久化到配置
        self.log_panel.printOnlyChanged.connect(self._on_print_only_setting)

        self.settings_tab.changed.connect(self._on_settings_changed)
        # 左上角「运行中流程」浮层读这份配置（设置页改动后 set_config 即时换样式）
        from .. import running_overlay
        running_overlay.set_config(self.cfg)
        manager.triggered.connect(self._dispatch_hotkey)

        # 初始化期间信号被各标签页守卫屏蔽，这里统一同步一次快照
        self._click_snapshot = self.clicker_tab.snapshot()
        self._press_snapshot = self.presser_tab.snapshot()
        self.cfg.clicker = self._click_snapshot
        self.cfg.presser = self._press_snapshot

        # 配置变化防抖保存
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.cfg.save)
        self.log_panel.clear_on_run = cfg.clear_log_on_run
        self.log_panel.print_only = cfg.log_print_only

        # ---- 中键菜单：两种触发方式（鼠标中键 / 全局快捷键）----
        # 状态必须在下面登记全局热键**之前**就绪：快捷键一注册，keyboard 的监听
        # 线程立刻生效，用户此时按下它就会直接进 show_middle_menu()，
        # 属性还没建好就会 AttributeError。顺序有源码级契约测试钉着，别调换。
        self._middle_menu_open = False      # 正在走「弹菜单」循环，防重入
        self._middle_menu = None            # 当前弹着的菜单（再次触发时要关掉它）
        self._pending_middle_pos = None     # 挂起的新位置：关掉旧菜单后据此重开
        self._pending_tool = ""             # 菜单里点了哪个九宫格工具（exec 返回后据此打开）
        # 中键"按下"已用它关过菜单时，抬起不再重开（见 MENU_SWALLOW_TRIGGER_SEC）
        self._menu_swallow_trigger_until = 0.0
        # 菜单开着期间临时装上的鼠标钩子（用户只开热键、没勾中键触发时才有值）
        self._menu_hook_temporary = False
        # 触发方式快照：只在「中键开关 / 快捷键」真的变了才重注册全部全局热键。
        # 增删改菜单项也会发 changed，但那些操作不影响任何热键，不该被打断。
        self._menu_trigger = (bool(cfg.middle_menu_enabled), cfg.middle_menu_hotkey)
        # 钩子线程的关闭请求（Esc）排队回主线程执行
        self._menuDismissRequested.connect(self._dismiss_middle_menu)

        self._register_hotkeys()

        # ---- 全局中键监听：中键抬起时在光标处弹出中键菜单 ----
        self.mouse_watcher = MouseMenuWatcher(self)
        self.mouse_watcher.middleClicked.connect(self._on_middle_click)
        # 菜单开着时任意鼠标键按下：全局钩子兜住"点外面就关"
        # （菜单弹在别的程序上时 Qt 弹窗拿不到捕获，见 _on_menu_button_pressed）
        self.mouse_watcher.menuButtonPressed.connect(self._on_menu_button_pressed)
        self.mouse_watcher.set_suppress(cfg.middle_menu_suppress)
        if cfg.middle_menu_enabled:
            self.mouse_watcher.start()

        # ---- 运行日志面板（底部可折叠，替代原左下角悬浮窗） ----
        from ..logbus import bus, log
        log("程序启动，所有模块就绪")
        bus().message.connect(self.log_panel.append)
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(500)
        self._overlay_timer.timeout.connect(self._update_overlay)
        self._overlay_timer.start()

        # 启动后延迟检查一次，之后每 1 小时循环检查（后台线程，不阻塞界面）
        QTimer.singleShot(2500, self._check_update)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)   # 1 小时
        self._update_timer.timeout.connect(self._check_update)
        self._update_timer.start()

    def _window_size(self) -> tuple[int, int]:
        """根据当前显示器可用区域（排除任务栏）动态计算窗口尺寸。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return _BASE_WINDOW
        geo = screen.availableGeometry()   # 排除任务栏的实际可用区域
        return auto_window_size(geo.width(), geo.height())

    # ---------- 文件拖放导入 ----------
    @staticmethod
    def _dropped_json_paths(mime) -> list[str]:
        """从拖放数据里取本地 .json 文件路径（含网络地址/非 json 的丢弃）。"""
        if not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = url.toLocalFile()
            if p.lower().endswith(".json"):
                out.append(p)
        return out

    def dragEnterEvent(self, ev) -> None:
        if self._dropped_json_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev) -> None:
        if self._dropped_json_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev) -> None:
        paths = self._dropped_json_paths(ev.mimeData())
        if not paths:
            super().dropEvent(ev)
            return
        ev.acceptProposedAction()
        # 切到自动化流程页再导入；逐个尝试，全部失败则汇总提示
        self.tabs.setCurrentWidget(self.flow_tab)
        failed = []
        for p in paths:
            if not self.flow_tab.import_flow_file(p):
                failed.append(os.path.basename(p))
        if failed:
            self.statusBar().showMessage(
                f"导入失败：{'、'.join(failed)} 不是有效的流程文件", 6000)

    # ---------- 日志面板 ----------
    def _on_log_expanded(self, expanded: bool) -> None:
        """日志展开时窗口整体增高、收缩时降低。

        展开优先向下增高；若底部会超出屏幕可用区域，则向上平移窗口
        （保持底部贴屏幕边缘），避免日志面板跑到屏幕外。
        最大化时不动窗口尺寸（那会把最大化状态改掉）。
        """
        if self.isMaximized():
            return
        from .log_panel import _TEXT_HEIGHT
        screen = self.screen() or QApplication.primaryScreen()
        delta = _TEXT_HEIGHT if expanded else -_TEXT_HEIGHT
        new_h = max(self.height() + delta, _MIN_WINDOW[1])
        if screen is not None:
            avail = screen.availableGeometry()
            bottom = self.y() + new_h
            if bottom > avail.bottom():
                # 超屏：向上挪，底部对齐屏幕可用区下沿
                self.move(self.x(), max(avail.top(), avail.bottom() - new_h))
        self.resize(self.width(), new_h)

    def _running_list(self) -> list[str]:
        """当前正在运行的任务条目（状态栏指示器 / 日志面板摘要共用）。"""
        parts = []
        if self.click_task.is_running:
            parts.append("鼠标连点")
        if self.press_task.is_running:
            parts.append("键盘连按")
        # 走各 tab 的公开方法，不直接翻它们的 _runners（那是内部实现）
        parts.extend(f"找图「{name}」" for name in self.finder_tab.running_names())
        parts.extend(f"流程「{name}」" for name in self.flow_tab.running_names())
        return parts

    def _running_summary(self) -> str:
        return " · ".join(self._running_list())

    def _update_overlay(self) -> None:
        tasks = self._running_list()
        self.log_panel.set_summary(" · ".join(tasks))
        # 状态栏运行指示器：无任务隐藏；有任务显示名称/数量，悬停弹出列表
        if tasks:
            n = len(tasks)
            text = f"▶ 运行中：{n} 个任务" if n > 1 else f"▶ 运行中：{tasks[0]}"
            if self.run_ind.text() != text:
                self.run_ind.setText(text)
            tip = ("正在运行 {n} 个任务：\n{body}\n\n（点击切换到自动化流程页）"
                   .format(n=n, body="\n".join(f"· {t}" for t in tasks)))
            if self.run_ind.toolTip() != tip:
                self.run_ind.setToolTip(tip)
            self.run_ind.show()
        else:
            self.run_ind.hide()

    def window_qss(self) -> str:
        """窗口级样式：圆角卡片 + 自绘标题栏（基类）+ 按钮 + 顶部导航栏。

        窗口级 QSS 优先级高于应用级基线（theme.build_base_qss），
        控件级内联样式（set_variant 等）优先级最高——三层配合，
        所以这里一次写入即可，不需要像以前那样「先按钮、再追加标签页」。
        """
        from . import theme
        return (super().window_qss()
                + theme.build_button_qss() + theme.build_tab_qss())

    def _apply_window_theme(self) -> None:
        """主题切换 / 最大化状态变化时重刷窗口级样式（含自绘标题栏）。"""
        self.apply_window_qss()
        # 齿轮是按当前主题的颜色**画**出来的像素（不吃 QSS 颜色重映射），换主题要重画
        button = getattr(self, "settings_btn", None)
        if button is not None:
            sync_settings_corner(button, self.tabs, self.settings_tab)

    def statusBar(self) -> QStatusBar:
        """卡片内的状态栏。

        `QMainWindow.statusBar()` **不是虚函数**，这里覆盖只是让 Python 侧的
        `self.statusBar().xxx`（本文件十几处）拿到卡片内那个，同时避免
        QMainWindow 在自己的窗口底部再留一条系统状态栏（那就跑到圆角外面去了）。
        """
        return self.status_bar()

    def _apply_tab_theme(self) -> None:
        """兼容旧调用点：顶部导航栏配色（现已并入 _apply_window_theme）。"""
        self._apply_window_theme()

    def _apply_button_theme(self) -> None:
        """兼容旧调用点：全局按钮配色（现已并入 _apply_window_theme）。"""
        self._apply_window_theme()

    # ---------- 快照与保存 ----------
    def _on_clicker_changed(self) -> None:
        self._click_snapshot = self.clicker_tab.snapshot()
        self.cfg.clicker = self._click_snapshot
        self._register_hotkeys()
        self._save_timer.start()

    def _on_presser_changed(self) -> None:
        self._press_snapshot = self.presser_tab.snapshot()
        self.cfg.presser = self._press_snapshot
        self._register_hotkeys()
        self._save_timer.start()

    def _on_finder_changed(self) -> None:
        self._register_hotkeys()
        self._save_timer.start()

    def _on_flow_changed(self) -> None:
        self._register_hotkeys()
        self._save_timer.start()

    def _on_flow_steps_changed(self) -> None:
        """仅步骤顺序/内容变化（拖动排序、步骤增删改）：只需防抖落盘。

        热键只由流程的 hotkey 字段决定，左栏条目只显示流程名，定时任务页与
        中键菜单页也只引用流程名——都与步骤无关，所以这里刻意不重注册热键、
        也不刷新那两个页面，避免每次拖放都做一轮无用功。
        """
        self._save_timer.start()

    def _on_flow_started(self, has_status_log: bool = True) -> None:
        """有流程开始运行：底部日志按开关清空；状态日志按需显示。

        只有流程内部**含有「状态日志」模块**时才显示浮层；否则隐藏，避免显示
        上一轮留下的空日志框（2026-10-01 用户要求）。内容不清空。
        """
        if self.log_panel.clear_on_run:
            self.log_panel.clear()
        from .. import running_overlay
        if has_status_log:
            running_overlay.reset_for_new_run()
        else:
            running_overlay.suppress_status()

    def _refresh_running_overlay(self) -> None:
        """把正在运行的流程（含来源/分组/热键）刷到屏幕上的悬浮窗。

        状态日志的清空不在这里做：它是「浮层重新打开时才清」（见
        running_overlay.StatusLogOverlay.append_status），与流程运行与否无关。
        """
        from .. import running_overlay
        running_overlay.refresh(self.flow_tab.running_overview())

    # ---------- 中键菜单 ----------
    def _on_middle_menu_changed(self) -> None:
        """中键菜单配置变化：同步监听器/热键，并触发防抖保存。

        菜单项增删改也会走到这里（同一个 changed 信号），但那种改动与
        「鼠标钩子开关」「快捷键」都无关，故只在触发方式真的变了时重注册热键
        ——unregister_all + 重注册会短暂卸掉所有全局热键，没必要白挨一次。
        """
        enabled = bool(self.cfg.middle_menu_enabled)
        self.mouse_watcher.set_suppress(bool(self.cfg.middle_menu_suppress))
        if enabled and not self.mouse_watcher.is_running():
            if not self.mouse_watcher.start():
                self.statusBar().showMessage("中键菜单启动失败（无法安装鼠标钩子）", 5000)
        elif not enabled and self.mouse_watcher.is_running():
            self.mouse_watcher.stop()
        trigger = (enabled, self.cfg.middle_menu_hotkey)
        if trigger != self._menu_trigger:
            self._menu_trigger = trigger
            self._register_hotkeys()        # 快捷键触发中键菜单：增删注册
            self._refresh_status_hint()
        self._save_timer.start()

    def _request_menu_dismiss(self) -> None:
        """钩子线程请求关闭中键菜单（Esc）→ 排队到主线程真正去关。

        钩子回调跑在输入线程里，绝不能直接碰 QWidget；`_menuDismissRequested`
        是跨线程信号，自动队列到主线程。
        """
        self._menuDismissRequested.emit()

    def _dismiss_middle_menu(self, reason: str = "") -> None:
        """关掉正在弹着的中键菜单，并且**不重开**（位置置空，外层循环随即退出）。"""
        if reason:
            from ..logbus import log as _log
            _log(f"中键菜单：关闭（{reason}）")
        self._pending_middle_pos = None
        if self._middle_menu is not None:
            self._middle_menu.close()

    def _menu_debug_report(self, text: str) -> None:
        """把中键菜单的判定过程写进运行日志（只在菜单开着时调用，不会刷屏）。

        为什么留着这段诊断：用户反馈过"某条路径下点外面不关"，而两条触发路径的代码
        完全同一个函数，只能靠现场数据定位断在哪一环（钩子没上报？判成了菜单内？
        关了没生效？）。日志里把**两套坐标**和**判定结果**都写出来。
        """
        try:
            from ..logbus import log as _log
            _log(text)
        except Exception:
            pass

    def _on_menu_button_pressed(self, x: int, y: int, button: str = "") -> None:
        """任意鼠标键按下（全局钩子上报）：菜单开着且点在**外面**就关掉菜单。

        为什么不让 Qt 自己处理：菜单弹在别的程序上面时，Windows 的前台锁定让我们
        的弹窗拿不到鼠标捕获/焦点，Qt 那套"点外面就关"根本不会触发——用户看到的
        就是「点其他位置菜单也不消失」。全局钩子不看捕获、也不看前台，一定收得到，
        所以这里兜一层。

        ⚠️ 判内外**必须用 `QCursor.pos()`（Qt 逻辑坐标）**，不能拿钩子上报的 x/y：
        鼠标钩子给的是**物理像素**，而 `menu.geometry()` 是**逻辑像素** —— 本机显示
        缩放 125%（dpr=1.24）时两者差 1.24 倍，菜单命中区被放大近四分之一，
        紧挨着菜单外面的点击会被当成"点在里面"而关不掉（2026-10-04 用户反馈
        「鼠标点外面还是不行」；实测逻辑 (1096,827) 对应物理 (1359,1025)）。

        ⚠️ **中键按下单独走一条**：中键是"开关"语义，按下这次就算关，并且要通知
        `_on_middle_click` **别再重开**。否则一次中键点在菜单外面会「按下关掉 → 抬起
        又开一个」，看起来就是"点外面关不掉"（用户反馈的"有时候"正是它，2026-10-04）。
        """
        menu = self._middle_menu
        if menu is None:
            return                  # 菜单没开：这个信号一律忽略（钩子是无条件上报的）
        if button == "middle":
            # 这次中键已经被"关菜单"用掉了；抬起时的触发信号不能再重开
            self._menu_swallow_trigger_until = time.monotonic() + \
                MENU_SWALLOW_TRIGGER_SEC
            self._dismiss_middle_menu(f"{button or '鼠标'}键按下（中键开关语义）")
            return
        try:
            geo = menu.geometry()
            cursor = QCursor.pos()
            inside = geo.contains(cursor)
        except Exception:
            geo, cursor, inside = None, None, False
        cursor_text = f"{cursor.x()},{cursor.y()}" if cursor is not None else "?"
        geo_text = geo.getRect() if geo is not None else "?"
        self._menu_debug_report(
            f"中键菜单：{button or '鼠标'}键按下，光标(逻辑) {cursor_text} | "
            f"菜单(逻辑) {geo_text} | 钩子(物理) {x},{y} → "
            + ("菜单内，交给 Qt" if inside else "菜单外，关闭"))
        if not inside:
            self._dismiss_middle_menu(f"{button or '鼠标'}键点在菜单外")

    def _on_middle_click(self, x: int, y: int) -> None:
        """全局中键抬起：在光标处弹出中键菜单（鼠标中键这一路触发）。

        菜单已经弹着时这一下就是"关"（见 show_middle_menu 的说明）——菜单弹在
        浏览器上时，只有全局钩子看得见的中键还能用来关闭。

        ⚠️ 但**按下那一下可能已经把关菜单做掉了**（点在菜单外面时会走
        `_on_menu_button_pressed` 的中键分支，并置上 swallow 时间戳）：这时抬起
        绝不能再去开一个新菜单，否则就是"关掉又立刻重开"= 看起来关不掉。
        """
        if not self.cfg.middle_menu_enabled:
            return
        if time.monotonic() < self._menu_swallow_trigger_until:
            self._menu_swallow_trigger_until = 0.0
            return
        self.show_middle_menu()

    def _on_middle_menu_tool(self, key: str) -> None:
        """菜单里点了九宫格工具：先记下来再关菜单。

        菜单里点工具**不能**靠 `menu.exec()` 的返回值回传——返回值只承载
        「被触发的 QAction」，而工具是 QWidgetAction 里的普通按钮。所以统一走这条
        回调：记进 `_pending_tool`，关掉菜单让 exec 返回，再由 `show_middle_menu`
        的循环统一打开（保证「关闭菜单」这件事只有一处做）。
        """
        self._pending_tool = str(key or "")
        if self._middle_menu is not None:
            self._middle_menu.close()

    def _open_middle_menu_tool(self, key: str) -> None:
        """打开菜单里选中的工具：与在「🧰 小工具」页点卡片走**同一条路径**。

        条目可能是内置小程序（开独立窗口），也可能是用户自定义添加的电脑里的程序
        （交给系统打开），统一由 `tool_entries.open_entry` 分发。
        """
        from ..tool_entries import find_entry, open_entry
        entry = find_entry(key, self.cfg.custom_tools)
        if entry is None:
            self.statusBar().showMessage(
                "中键菜单：该工具已不存在（可能已被移除），请重新配置", 4000)
            return
        open_entry(entry, anchor=self)

    def show_middle_menu(self, source: str = "中键") -> None:
        """在光标处弹出中键菜单，选中条目则运行对应流程。

        两种触发方式共用（鼠标中键抬起 / 用户设置的全局快捷键），运行在 Qt
        主线程。若已有关闭中的模态对话框则不打扰。

        **菜单已经弹着时再触发一次 = 关掉它**（不再像以前那样"关掉旧的、在新光标处
        重开"）。为什么改（2026-10-04 用户两次反馈「关不掉菜单」）：
        - 中键/热键是**唯一还能被全局钩子看见的关闭手势**：菜单弹在浏览器等程序
          上面时，Windows 的前台锁定让我们的弹窗拿不到鼠标捕获与键盘焦点，
          Qt 那套"Esc / 点外面就关"根本不会触发（Esc 甚至永久阻塞 exec）；
        - 而原来的"又关又开"让这个手势等于失效，用户按多少次都关不掉，还会每次
          让浏览器顺手执行中键的默认行为。
        想换位置：关掉后在别处再触发一次即可（两下，但永远关得掉）。

        另外还给两条 Qt 原生的关闭路径上了全局钩子兜底（Esc 监听 + 鼠标键上报），
        它们不依赖弹窗有没有拿到焦点，见 `_request_menu_dismiss` 与
        `_on_menu_button_pressed`。
        """
        if self._middle_menu is not None:
            self._dismiss_middle_menu()      # 再触发一次就是"关"，且不重开
            return
        if self._middle_menu_open:      # 循环正在重开的空档（此间不跑事件循环）
            return
        if QApplication.activeModalWidget() is not None:
            return
        # 只要「九宫格工具」或「流程菜单项」还有一样，菜单就值得弹
        if not self.cfg.middle_menu_items and not self.cfg.middle_menu_tools:
            return
        self._middle_menu_open = True
        self.mouse_watcher.set_menu_open(True)   # 开着期间吞中键，见 docstring
        self._pending_middle_pos = QCursor.pos()
        # 「点外面关菜单」靠的是鼠标钩子。若用户只开了热键（没勾「鼠标中键触发」），
        # 钩子本来就没装 —— 那样热键打开的菜单就**永远关不掉**（点外面、点条目都
        # 只能靠 Qt，而弹窗在别的程序上面时 Qt 收不到）。所以菜单开着期间**临时装上**，
        # 收起后还原成原来的状态（2026-10-04 用户实测：没勾选就无法用鼠标关闭）。
        self._menu_hook_temporary = not self.mouse_watcher.is_running()
        if self._menu_hook_temporary:
            self.mouse_watcher.start()
        self._menu_debug_report(
            f"中键菜单：已由「{source}」弹出（光标 {self._pending_middle_pos.x()},"
            f"{self._pending_middle_pos.y()}），鼠标钩子"
            f"{'临时启动' if self._menu_hook_temporary else '本来就在跑'}")
        try:
            while self._pending_middle_pos is not None:
                pos = self._pending_middle_pos
                # 先清空：只有「这一轮弹窗期间」再次触发的才算新位置
                self._pending_middle_pos = None
                menu = build_menu(self.cfg.middle_menu_items, self.cfg.flows, self,
                                  tools=self.cfg.middle_menu_tools,
                                  on_tool=self._on_middle_menu_tool,
                                  custom_tools=self.cfg.custom_tools)
                if menu is None:
                    self.statusBar().showMessage(
                        "中键菜单：没有可运行的菜单项"
                        "（关联流程可能已被删除，且没配置九宫格工具）", 4000)
                    break
                self._middle_menu = menu
                try:
                    # Esc 用**全局**监听兜住：菜单弹在别的程序上面时（用户最后那次输入
                    # 落在它那儿，Windows 前台锁定让我们拿不到键盘焦点），Qt 自己的
                    # Esc 处理收不到键，菜单就关不掉了。钩子不看焦点，一定收得到。
                    with cancel_key.EscListener(self._request_menu_dismiss):
                        chosen = menu.exec(pos)
                    # 必须在 deleteLater 之前把 data 取出来——菜单一删 QAction 也没了
                    flow_id = str(chosen.data() or "") if chosen is not None else ""
                    tool_key = self._pending_tool
                    self._pending_tool = ""
                finally:
                    self._middle_menu = None
                    if self._menu_hook_temporary:
                        # 临时装上的钩子收走，恢复成"用户只开热键"的原始状态
                        self._menu_hook_temporary = False
                        self.mouse_watcher.stop()
                    menu.deleteLater()   # 菜单挂在 self 名下，不删会每触发一次积一个
                if tool_key:             # 点了九宫格工具 → 打开它，本轮结束
                    self._open_middle_menu_tool(tool_key)
                    break
                if not flow_id:
                    continue             # 可能只是又被触发了一次 → 回循环看有无新位置
                self._run_flow_from_middle_menu(flow_id)
                break                    # 选中条目即结束；挂起的按键不再理会
        finally:
            self._middle_menu = None
            self._pending_middle_pos = None
            self._pending_tool = ""
            self._middle_menu_open = False
            self._menu_hook_temporary = False
            self.mouse_watcher.set_menu_open(False)   # 收起即恢复中键的正常行为

    def _run_flow_from_middle_menu(self, flow_id: str) -> None:
        """运行中键菜单选中的流程：已在运行/排队则跳过，不打断用户手动运行。

        同步流程（默认）遇忙会排队，所以「已启动」要区分是真的在跑还是进了队列。
        """
        flow = next((f for f in self.cfg.flows if f.id == flow_id), None)
        if flow is None:
            return
        if not flow.steps:
            self.statusBar().showMessage(f"「{flow.name}」还没有步骤，无法运行", 5000)
            return
        if self.flow_tab.start_flow_if_idle(flow_id, silent=True):
            if self.flow_tab.is_queued(flow_id):
                self.statusBar().showMessage(
                    f"中键菜单：「{flow.name}」已加入排队，等前面的流程结束后自动开始", 6000)
            else:
                self.statusBar().showMessage(f"中键菜单：已启动「{flow.name}」", 5000)
        else:
            self.statusBar().showMessage(f"「{flow.name}」已在运行或排队中，已跳过", 4000)

    def _on_clear_log_setting(self, checked: bool) -> None:
        """「每次运行清空日志」勾选状态变化：持久化到配置。"""
        self.cfg.clear_log_on_run = bool(checked)
        self._save_timer.start()

    def _on_print_only_setting(self, checked: bool) -> None:
        """「只显示打印输出」勾选状态变化：持久化到配置。"""
        self.cfg.log_print_only = bool(checked)
        self._save_timer.start()

    def _refresh_status_hint(self) -> None:
        """底部状态栏热键提示，随设置实时刷新。"""
        toggle_hk = hotkey_display(self.cfg.show_hide_hotkey) or "未设置"
        stop_hk = hotkey_display(self.cfg.stop_all_hotkey) or "未设置"
        text = f"显示/隐藏窗口：{toggle_hk}    紧急停止：{stop_hk}"
        menu_hk = hotkey_display(self.cfg.middle_menu_hotkey)
        if menu_hk:
            text += f"    快捷菜单：{menu_hk}"
        self.status_hint.setText(text)

    def _on_settings_changed(self) -> None:
        toggle_hk, stop_hk = self.settings_tab.values()
        self.cfg.show_hide_hotkey = toggle_hk
        self.cfg.stop_all_hotkey = stop_hk
        # 界面主题与全局字体百分比（都已在设置页里实时应用，这里只负责写回配置
        # → 防抖保存持久化）
        self.cfg.ui_theme = self.settings_tab.theme_value()
        self.cfg.ui_font_scale = self.settings_tab.font_scale_value()
        # 运行状态浮层外观（标题/流程名称各自的字号字体颜色 + 背景/位置/开关）：
        # 写回配置并让可见浮层立即换样式
        ov = self.settings_tab.overlay_values()
        self.cfg.run_overlay_enabled = ov["enabled"]
        for kind in ("title", "flow"):
            setattr(self.cfg, f"run_overlay_{kind}_font_size",
                    ov[f"{kind}_font_size"])
            setattr(self.cfg, f"run_overlay_{kind}_font_family",
                    ov[f"{kind}_font_family"])
            setattr(self.cfg, f"run_overlay_{kind}_text_color",
                    ov[f"{kind}_text_color"])
        self.cfg.run_overlay_bg_color = ov["bg_color"]
        self.cfg.run_overlay_pos = ov["pos"]
        # 状态日志（浮层透明控制台）的字体/字号/三级颜色/最多行数
        self.cfg.run_overlay_log_font_size = ov["log_font_size"]
        self.cfg.run_overlay_log_font_family = ov["log_font_family"]
        self.cfg.run_overlay_log_color = ov["log_color"]
        self.cfg.run_overlay_log_warn_color = ov["log_warn_color"]
        self.cfg.run_overlay_log_error_color = ov["log_error_color"]
        self.cfg.run_overlay_log_max_lines = ov["log_max_lines"]
        # 状态日志浮层是独立窗口：开关 / 位置 / 背景
        self.cfg.run_overlay_log_enabled = ov["log_enabled"]
        # ⚠️ 旧值必须在赋值**之前**留住：紧跟着那行已经把 cfg 改成了新位置，
        # 再拿新值跟它自己比永远相等，条件恒为假 → 在设置页重选位置后，
        # 之前手动拖动记下的 custom_pos 永远清不掉，九宫格设置不生效（2026-10-02 review）。
        old_log_pos = self.cfg.run_overlay_log_pos
        self.cfg.run_overlay_log_pos = ov["log_pos"]
        self.cfg.run_overlay_log_bg_color = ov["log_bg_color"]
        self.cfg.run_overlay_log_bg_transparent = ov["log_bg_transparent"]
        self.cfg.run_overlay_log_max_width = ov["log_max_width"]
        self.cfg.run_overlay_log_max_height = ov["log_max_height"]
        self.cfg.run_overlay_log_auto_hide_sec = ov["log_auto_hide_sec"]
        # 在设置页重新选了坐标位置 → 清掉手动拖动记下的位置（让九宫格设置生效）
        if ov["log_pos"] != old_log_pos:
            self.cfg.run_overlay_log_custom_pos = ""
        from .. import running_overlay
        running_overlay.set_config(self.cfg)
        self._refresh_status_hint()
        self._register_hotkeys()
        self._save_timer.start()

    # ---------- 热键注册与调度 ----------
    def _register_hotkeys(self) -> None:
        self._dispatch = {}
        conflicts: list[str] = []

        def bind(hotkey: str, fn) -> None:
            hk = HotkeyManager.normalize(hotkey)
            if not hk:
                return
            if hk in self._dispatch:
                conflicts.append(hotkey_display(hk))
                return
            self._dispatch[hk] = fn

        bind(self.cfg.show_hide_hotkey, self.toggle_show_hide)
        bind(self.cfg.stop_all_hotkey, self.stop_all)
        # 每个分组一个热键：按一下运行**本组的异步流程**，再按一下停止本组异步流程
        # （开关语义）。用 lambda 包一层：绑定时不解析 flow_tab 的属性
        # （替身/裁剪过的窗口也能注册）
        for name, hk in (getattr(self.cfg, "group_hotkeys", {}) or {}).items():
            bind(hk, (lambda g: lambda: self.flow_tab.toggle_group_async(g))(name))
        # 热键这条单独标出来源：诊断日志要能区分"中键唤起"还是"热键唤起"
        # （用户反馈过"热键打开的菜单点外面不关"，而两条路是同一个函数）
        bind(self.cfg.middle_menu_hotkey,
             lambda: self.show_middle_menu(source="热键"))
        bind(self.cfg.clicker.hotkey, self.toggle_clicker)
        bind(self.cfg.presser.hotkey, self.toggle_presser)
        for t in self.cfg.find_tasks:
            hk = HotkeyManager.normalize(t.hotkey)
            if hk:
                bind(t.hotkey, (lambda tid: lambda: self.finder_tab.toggle_task(tid))(t.id))
        for f in self.cfg.flows:
            hk = HotkeyManager.normalize(f.hotkey)
            if hk:
                bind(f.hotkey, (lambda fid: lambda: self.flow_tab.toggle_flow(fid))(f.id))

        self.manager.unregister_all()
        failed = []
        for hk in self._dispatch:
            if not self.manager.register(hk):
                failed.append(hotkey_display(hk))
        if conflicts:
            self.statusBar().showMessage(f"热键冲突，已忽略：{'、'.join(conflicts)}", 5000)
        if failed:
            self.statusBar().showMessage(f"以下热键注册失败：{'、'.join(failed)}", 5000)

    def _dispatch_hotkey(self, hk: str) -> None:
        fn = self._dispatch.get(HotkeyManager.normalize(hk))
        if fn:
            fn()

    # ---------- 功能启停 ----------
    def toggle_clicker(self) -> None:
        if self.click_task.is_running:
            self.click_task.stop()
        else:
            self.clicker_tab.status.set_running()
            self.click_task.start()

    def toggle_presser(self) -> None:
        if self.press_task.is_running:
            self.press_task.stop()
        else:
            self.presser_tab.status.set_running()
            self.press_task.start()

    def stop_all(self) -> None:
        self.click_task.stop()
        self.press_task.stop()
        self.finder_tab.stop_all()
        self.flow_tab.stop_all()

    def toggle_all_finder(self) -> None:
        if self.finder_tab.any_running():
            self.finder_tab.stop_all()
        else:
            self.finder_tab.start_enabled_all()

    def _on_click_state(self, state: str, reason: str) -> None:
        self.clicker_tab.set_running(state == "running", reason)

    def _on_press_state(self, state: str, reason: str) -> None:
        self.presser_tab.set_running(state == "running", reason)

    # ---------- 在线更新 ----------
    def _check_update(self) -> None:
        """后台线程拉取远端版本号（无网络/仓库不可达时静默跳过）。

        由定时器驱动：启动后首次 + 每 1 小时一次。已有一次检查在途时不重复起。
        """
        if getattr(self, "_version_fetcher", None) is not None \
                and self._version_fetcher.isRunning():
            return
        self._version_fetcher = VersionFetcher()
        self._version_fetcher.fetched.connect(self._on_version_fetched)
        self._version_fetcher.start()

    def _on_version_fetched(self, source) -> None:
        remote, download_urls = source if isinstance(source, tuple) else (None, [])
        if not remote or not download_urls:
            logging.getLogger(__name__).info("检查更新：未获取到远端版本，跳过")
            return
        local = self.cfg.version or "1.0.0"
        if compare_versions(local, remote) >= 0:
            # local / remote 可能是 "v3.0.2" 这种带 v 前缀的 tag 原文，不要再拼 v
            logging.getLogger(__name__).info("检查更新：当前 %s 已是最新（远端 %s）",
                                             local, remote)
            return
        # 已有更新流程（待下载 / 下载中 / 已就绪 / 失败待重试）时不重复触发
        if self._update_state != "idle":
            return
        logging.getLogger(__name__).info("发现新版本：%s -> %s，等待用户手动下载",
                                         local, remote)
        # 手动下载：红点 + 提示文字 + 「下载更新」按钮，用户点击才开始下载
        self._pending_update = (remote, download_urls)
        self._set_update_state("available")

    def _start_download(self, download_urls: list[str]) -> None:
        """后台线程下载新版本到本地（由用户点击「下载更新」/「重新下载」触发）。"""
        self._downloader = AutoDownloader(download_urls)
        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.completed.connect(self._on_download_completed)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        """下载中实时刷新状态栏进度条（仅在展开时可见）。"""
        if total > 0:
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(min(100, int(done * 100 / total)))
            self.update_progress.setFormat(
                f"{done / 1048576:.0f} / {total / 1048576:.0f} MB")
        else:
            self.update_progress.setRange(0, 0)   # 服务器未返回大小：滚动不定进度
            self.update_progress.setFormat(f"{done / 1048576:.0f} MB")

    def _on_download_completed(self, file: str) -> None:
        self._downloaded_file = file
        self._update_state = "ready"
        self._set_update_state("ready")
        logging.getLogger(__name__).info("新版本已下载到本地：%s", file)

    def _on_download_failed(self, err: str) -> None:
        self._update_fail_reason = err
        self._update_state = "failed"
        self._set_update_state("failed")
        logging.getLogger(__name__).warning("下载新版本失败：%s", err)

    def _set_update_state(self, state: str) -> None:
        """按状态刷新状态栏左下角的红点 / 提示文字 / 按钮 / 进度条。"""
        self._update_state = state
        if state == "idle":
            self.update_dot.hide()
            self.update_hint.hide()
            self.update_btn.hide()
            self.update_progress.hide()
            self._progress_visible = True   # 重置为默认展开
            return
        remote = (self._pending_update or ("", []))[0]
        self.update_dot.show()
        self.update_hint.show()
        self.update_btn.show()
        if state == "available":
            self.update_hint.setText(f"发现新版本 {remote}")
            self.update_btn.setText("下载更新")
            self.update_btn.setEnabled(True)
            self.update_progress.hide()
            self._progress_visible = True   # 开始下载后默认展开
        elif state == "downloading":
            self.update_hint.setText(f"正在下载新版本 {remote}…")
            self.update_btn.setText("下载中…")
            self.update_btn.setEnabled(True)   # 可点击：收起/展开实时进度条
            self._progress_visible = True       # 默认展开进度条，点击可收起
            self.update_progress.setVisible(True)
        elif state == "ready":
            self.update_hint.setText(f"新版本 {remote} 已下载")
            self.update_btn.setText("重启升级")
            self.update_btn.setEnabled(True)
            self.update_progress.hide()
            self._progress_visible = True   # 下次下载默认展开
        elif state == "failed":
            self.update_hint.setText(f"新版本 {remote} 下载失败")
            self.update_btn.setText("重新下载")
            self.update_btn.setEnabled(True)
            self.update_progress.hide()
            self._progress_visible = True

    def _restart_upgrade(self) -> None:
        """左下角按钮：待下载=开始手动下载；下载中点击=展开/收起进度条；
        已就绪=替换 exe 并重启；失败=重新下载。"""
        if self._update_state in ("available", "failed"):
            urls = (self._pending_update or ("", []))[1]
            self._set_update_state("downloading")   # 内部默认展开进度条
            self._start_download(urls)
            return
        if self._update_state == "downloading":
            self._progress_visible = not self._progress_visible
            self.update_progress.setVisible(self._progress_visible)
            return
        if self._update_state == "ready" and self._downloaded_file:
            ok, why = install_update(self._downloaded_file)
            if not ok:
                QMessageBox.warning(self, "更新失败", why)
                return
            remote = (self._pending_update or ("", []))[0]
            if remote:
                self.cfg.version = remote
                self.cfg.save()
            logging.getLogger(__name__).info("重启升级：已替换为 %s，程序退出", remote)
            self.shutdown()
            QApplication.quit()

    # ---------- 窗口显隐 ----------
    def _hide_for_capture(self) -> None:
        """截屏取模前隐藏自己，避免主窗口被截进模板。"""
        self._was_visible_before_capture = self.isVisible()
        if self._was_visible_before_capture:
            self.hide()

    def _restore_after_capture(self) -> None:
        """取模结束（保存或取消）后恢复窗口。"""
        if getattr(self, "_was_visible_before_capture", False):
            self._was_visible_before_capture = False
            self.show()
            self.raise_()
            self.activateWindow()

    def show_window(self) -> None:
        """显示并置顶到前台。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._force_foreground()

    def hide_window(self) -> None:
        self._clear_topmost()       # 隐藏前解除置顶，下次显示从正常 z 序开始
        self.hide()

    def toggle_show_hide(self) -> None:
        """显示/隐藏切换键：隐藏时显示；已显示但不在前台时置顶；已在前台时隐藏。"""
        if not self.isVisible() or self.isMinimized():
            self.show_window()
        elif self.isActiveWindow():
            self.hide_window()
        else:
            self.show_window()

    def _force_foreground(self) -> None:
        """把窗口顶到屏幕最前面。

        Qt.Tool 窗口从后台 activateWindow 常被系统拒绝（只闪烁不置前），
        这里用 Win32：TOPMOST + AttachThreadInput 借用前台线程权限。

        两个关键点（都是实测踩过的坑，2026-10-02）：
        1. **ctypes 必须设 argtypes 并用 c_void_p 传句柄**：不设时 HWND_TOPMOST(-1)
           会被当 32 位 c_int 传，SetWindowPos 拿到的是 0x00000000FFFFFFFF（而非
           0xFFFFFFFFFFFFFFFF），等于没置顶（实测 EXSTYLE 的 WS_EX_TOPMOST 位不变）。
           这正是「可见但被遮挡时按快捷键没置顶」的真根因——TOPMOST 从来没设上去过。
        2. TOPMOST 不搞定时取消：SetForegroundWindow 受前台锁定限制可能被拒，
           被拒时窗口必须停在最前可见（失焦时解除，见 _on_activation_changed）。
        """
        self.raise_()
        self.activateWindow()
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            SWP = 0x1 | 0x2  # SWP_NOSIZE | SWP_NOMOVE

            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.restype = ctypes.c_int
            user32.AttachThreadInput.argtypes = [
                ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
            user32.AttachThreadInput.restype = ctypes.c_int
            kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

            hwnd_val = int(self.winId())
            hwnd = ctypes.c_void_p(hwnd_val)
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, SWP)  # TOPMOST
            self._pinned_topmost = True

            fg = user32.GetForegroundWindow()
            if fg and fg.value != hwnd_val:
                # 前台窗口可能恰为空（切换瞬间 fg=0）：没线程可借，直接调用即可，
                # 失败也有上面的 TOPMOST 兜底，窗口照样在最前可见。
                fg_tid = user32.GetWindowThreadProcessId(fg, None)
                # 附加「拥有本窗口的线程」（GUI 线程）而不是当前执行线程——
                # 这才是 AttachThreadInput 的正确姿势。
                my_tid = user32.GetWindowThreadProcessId(hwnd, None) \
                    or kernel32.GetCurrentThreadId()
                if fg_tid and fg_tid != my_tid:
                    user32.AttachThreadInput(my_tid, fg_tid, True)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(my_tid, fg_tid, False)
                else:
                    user32.SetForegroundWindow(hwnd)
        except Exception:
            logging.getLogger(__name__).debug("强制置前失败", exc_info=True)

    def _clear_topmost(self) -> None:
        """解除 TOPMOST，让窗口回到正常 z 序（失焦 / 隐藏时调用）。"""
        self._pinned_topmost = False
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            user32.SetWindowPos(ctypes.c_void_p(int(self.winId())),
                                ctypes.c_void_p(-2), 0, 0, 0, 0, 0x1 | 0x2)  # NOTOPMOST
        except Exception:
            logging.getLogger(__name__).debug("解除置顶失败", exc_info=True)

    def changeEvent(self, ev) -> None:      # noqa: N802（Qt 命名）
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.ActivationChange:
            self._on_activation_changed(self.isActiveWindow())

    def _on_activation_changed(self, active: bool) -> None:
        """置顶后一旦失焦就解除 TOPMOST（抽出来便于离屏测试）。"""
        # SetForegroundWindow 成功后用户切走 -> 失焦 -> 取消，回到正常 z 序；
        # 被拒绝时窗口保持 TOPMOST 可见，用户点它激活 -> 之后再切走 -> 同样解除。
        if getattr(self, "_pinned_topmost", False) and not active:
            self._clear_topmost()

    def closeEvent(self, ev) -> None:
        # 点 X 隐藏到托盘，不退出；退出走托盘菜单
        ev.ignore()
        self.hide_window()
        self.hideToTrayNotice.emit()

    def shutdown(self) -> None:
        from ..overlay_actor import close_all as close_floating_images
        from ..power_overlay import close_all as close_power_countdown
        from .. import running_overlay
        from .mini_window import close_all as close_mini_windows
        close_mini_windows()           # 关掉还开着的小工具独立窗口
        close_floating_images()        # 销毁还留在桌面上的悬浮图片
        close_power_countdown()        # 销毁可能还留在屏幕下方的关机倒计时浮层
        running_overlay.close()        # 销毁左上角「运行中流程」红色浮层
        self.schedule_tab.shutdown()   # 停止定时任务调度线程
        self.mouse_watcher.stop()      # 卸载全局鼠标钩子
        self.stop_all()
        self.manager.unregister_all()
