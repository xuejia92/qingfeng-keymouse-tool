import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

import tempfile

import app.config as _cfgmod
_cfgmod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "qf_test_flow2.json")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from app.config import AppConfig, Flow, FlowStep
from app.hotkey_manager import HotkeyManager
from app.ui.flow_dialog import FlowMetaDialog, StepParamsDialog
from app.ui.main_window import MainWindow

app = QApplication([])
cfg = AppConfig()
cfg.flows = [Flow(name="签到流程")]
win = MainWindow(cfg, HotkeyManager())
win.resize(1000, 620)
win.show()
win.centralWidget().setCurrentWidget(win.flow_tab)
ft = win.flow_tab
app.processEvents()

print("[1] 左栏流程数:", ft.list.count(), "(expect 1)")
print("[2] 右栏标题:", ft.right_title.text())
print("[3] 空流程占位:", ft.step_list.count(), "行 (expect 1 行占位)")

# ---- 模块拖入（事件级 dropEvent 模拟）----
from PySide6.QtCore import QMimeData, QPoint
from PySide6.QtGui import QDropEvent
from app.ui.flow_dialog import MIME_TYPE


def drop(step_type, y):
    mime = QMimeData()
    mime.setData(MIME_TYPE, step_type.encode())
    ft.step_list.dropEvent(QDropEvent(QPoint(10, y), Qt.MoveAction, mime,
                                      Qt.LeftButton, Qt.NoModifier))


drop("click", 20)
drop("wait", 60)
drop("press", 100)
app.processEvents()
print("[4] 拖入3步:", [s.type for s in ft._selected_flow().steps], "(expect click/wait/press)")
print("[5] 右栏行数:", ft.step_list.count(), "(expect 3)")

# ---- 排序（内部移动等价路径：takeItem/insert + orderChanged）----
it = ft.step_list.takeItem(0)
ft.step_list.insertItem(2, it)
ft.step_list.orderChanged.emit()
QTimer.singleShot(50, lambda: None)
app.processEvents()
import time
time.sleep(0.1); app.processEvents()
print("[6] 排序后:", [s.type for s in ft._selected_flow().steps], "(expect wait/press/click)")

# ---- 参数编辑（wait 步骤直接走对话框 API）----
ft.step_list.setCurrentRow(0)
step = ft._current_step()
dlg = StepParamsDialog(step)
dlg.seconds.setValue(2.5)
dlg.apply_to(step)
print("[7] wait 参数改 2.5s:", ft._selected_flow().steps[0].params["seconds"], "(expect 2.5)")

# ---- 运行锁定 ----
lock_state = {}
def check_locked():
    lock_state["list_disabled"] = not ft.step_list.isEnabled()
    lock_state["btn_disabled"] = not ft._module_btns[0].isEnabled()
    lock_state["title"] = ft.panel_box.title()
def stop_it():
    ft.stop_all()
def check_unlocked():
    print("[8] 运行中列表锁定:", lock_state.get("list_disabled"), "(expect True)")
    print("[9] 运行中模块按钮锁定:", lock_state.get("btn_disabled"), "(expect True)")
    print("[10] 锁定标题:", lock_state.get("title"))
    print("[11] 停止后列表解锁:", ft.step_list.isEnabled(), "(expect True)")
    print("[12] 停止后标题:", ft.panel_box.title())
    app.quit()

results = {}
def on_state(s, r):
    if s == "stopped":
        check_unlocked()

ft.runningStateChanged.connect(lambda: None)
runner_state = []
def watch():
    pass

# 拦截 _on_state 的弹窗不可控，这里手动驱动：直接用 runner 信号验证锁定
flow_id = cfg.flows[0].id
ft.toggle_flow(flow_id)
QTimer.singleShot(400, check_locked)
QTimer.singleShot(600, stop_it)
QTimer.singleShot(1400, check_unlocked)
QTimer.singleShot(2000, app.quit)
QTimer.singleShot(2000, lambda: results.update(done=True))
app.exec()

# ---- 元信息对话框 ----
f2 = Flow(name="x", loops=2, hotkey="f10")
md = FlowMetaDialog(f2, create=True)
md.name_edit.setText("发帖流程")
md.loops_spin.setValue(3)
md.hotkey_edit.set_hotkey("f11")
md.apply_to(f2)
print("[13] 元信息对话框:", f2.name, f2.loops, f2.hotkey, "(expect 发帖流程 3 f11)")

# ---- 渲染 ----
ft.list.setCurrentRow(0)
app.processEvents()
ft.grab().save(".zcode/flow_new_layout.png")
print("rendered, ALL DONE")
