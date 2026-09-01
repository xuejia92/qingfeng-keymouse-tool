import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

import tempfile

import app.config as _cfgmod
_cfgmod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "qf_e2e2.json")

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.config import AppConfig, Flow, FlowStep
from app.hotkey_manager import HotkeyManager
from app.ui.flow_dialog import StepParamsDialog
from app.ui.main_window import MainWindow

app = QApplication([])
cfg = AppConfig()
cfg.flows = [Flow(name="E2E2", steps=[FlowStep(type="find")])]
win = MainWindow(cfg, HotkeyManager())
win.show()
ft = win.flow_tab
ft.step_list.setCurrentRow(0)
step = ft._current_step()
app.processEvents()

dlg = StepParamsDialog(step, ft)
dlg.templateCaptureRequested.connect(lambda: ft._capture_template_for_step(dlg))
log = {}


def drive_overlay():
    from app.capture_overlay import CaptureOverlay
    ovs = [w for w in QApplication.topLevelWidgets() if isinstance(w, CaptureOverlay)]
    log["overlay"] = len(ovs)
    if not ovs:
        return
    ov = ovs[0]
    QTest.mousePress(ov, Qt.LeftButton, pos=QPoint(60, 60))
    QTest.mouseMove(ov, QPoint(280, 240))
    QTest.mouseRelease(ov, Qt.LeftButton, pos=QPoint(280, 240))
    app.processEvents()
    QTest.mouseDClick(ov, Qt.LeftButton, pos=QPoint(170, 150))
    app.processEvents()


def click_ok():
    log["image_in_dialog"] = getattr(dlg, "_image", None)
    dlg.accept()   # 模拟用户点 OK


def after_exec():
    pass


QTimer.singleShot(100, dlg.show)
QTimer.singleShot(200, dlg._request_capture)
QTimer.singleShot(900, drive_overlay)
QTimer.singleShot(2400, click_ok)
QTimer.singleShot(7000, app.quit)

res = dlg.exec()   # 真实模态循环
print("exec 结果:", res, "(1=Accepted 即用户点了 OK)")
print("对话框内 image:", log.get("image_in_dialog"))
if res == StepParamsDialog.Accepted:
    dlg.apply_to(step)   # FlowTab 的真实后续动作
print("apply 后 step image:", repr(step.params.get("image")))
print("apply 后 step image_path:", repr(step.params.get("image_path")))
