import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

import tempfile

import app.config as _cfgmod
_cfgmod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "qf_e2e.json")

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.config import AppConfig, Flow, FlowStep
from app.hotkey_manager import HotkeyManager
from app.ui.flow_dialog import StepParamsDialog
from app.ui.main_window import MainWindow

app = QApplication([])
cfg = AppConfig()
cfg.flows = [Flow(name="E2E", steps=[FlowStep(type="find")])]
win = MainWindow(cfg, HotkeyManager())
win.show()
ft = win.flow_tab
app.processEvents()

log = []


def overlay_of_type():
    from app.capture_overlay import CaptureOverlay
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, CaptureOverlay)]


def stage1():
    # 打开找图步骤参数对话框，点"屏幕截图选区"
    ft.step_list.setCurrentRow(0)
    step = ft._current_step()
    dlg = StepParamsDialog(step, ft)
    dlg.templateCaptureRequested.connect(lambda: ft._capture_template_for_step(dlg))
    dlg.show()
    app.processEvents()
    dlg._request_capture()
    app.processEvents()
    log.append(("dialog_hidden", not dlg.isVisible()))


def stage2():
    ovs = overlay_of_type()
    log.append(("overlay_created", len(ovs)))
    if not ovs:
        app.quit(); return
    ov = ovs[0]
    # 拖拽一个 100x80 的选区（在遮罩坐标系内）
    QTest.mousePress(ov, Qt.LeftButton, pos=QPoint(50, 50))
    QTest.mouseMove(ov, QPoint(300, 260))
    QTest.mouseRelease(ov, Qt.LeftButton, pos=QPoint(300, 260))
    app.processEvents()
    log.append(("adjusting", ov._state))
    # 双击确认
    QTest.mouseDClick(ov, Qt.LeftButton, pos=QPoint(175, 155))
    app.processEvents()
    log.append(("overlay_closed", all(not o.isVisible() for o in overlay_of_type())))
    for w in QApplication.topLevelWidgets():
        if isinstance(w, StepParamsDialog):
            log.append(("dialog_visible_again", w.isVisible()))
            log.append(("dialog_image", getattr(w, "_image", None)))


def stage3():
    for w in QApplication.topLevelWidgets():
        if isinstance(w, StepParamsDialog):
            w.accept()
    # FlowTab 的 apply 逻辑：手动执行（等价 exec Accepted 分支）
    step = ft._current_step()
    app.processEvents()
    log.append(("step_params", dict(step.params)))
    app.quit()


QTimer.singleShot(100, stage1)
QTimer.singleShot(800, stage2)
QTimer.singleShot(2000, stage3)
QTimer.singleShot(6000, app.quit)
app.exec()

for k, v in log:
    print(f"{k}: {v}")
