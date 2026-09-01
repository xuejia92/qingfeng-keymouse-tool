import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

import tempfile

import app.config as _cfgmod
_cfgmod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "qf_test_hk3.json")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.config import AppConfig, Flow, FlowStep
from app.hotkey_manager import HotkeyManager
from app.ui.flow_dialog import StepParamsDialog
from app.ui.main_window import MainWindow

app = QApplication([])
cfg = AppConfig()
cfg.flows = [Flow(name="每日签到", hotkey="f10",
                  steps=[FlowStep(type="wait", params={"seconds": 1}),
                         FlowStep(type="find", params={"image": ""}),
                         FlowStep(type="click", params={"count": 5})]),
             Flow(name="自动发帖", loops=3)]
win = MainWindow(cfg, HotkeyManager())
win.resize(1000, 620)
win.show()
win.centralWidget().setCurrentWidget(win.flow_tab)
ft = win.flow_tab
app.processEvents()

# ---- 左栏三行显示 ----
it = ft.list.item(0)
lines = it.text().split("\n")
print("[1] 左栏行数:", len(lines), "(expect 3)")
print("[2] 三行内容:", lines)
print("[3] 第二个流程:", ft.list.item(1).text().replace("\n", " | "))
print("[4] 字体点数:", ft.list.font().pointSize(), "(QSS 9pt 生效则以渲染为准)")

# ---- 找图步骤：模板图截图选区链路 ----
ft.step_list.setCurrentRow(1)  # find 步骤
step = ft._current_step()
print("[5] 选中步骤类型:", step.type)
dlg = StepParamsDialog(step, ft)
captured_req = {"v": False}
dlg.templateCaptureRequested.connect(lambda: captured_req.__setitem__("v", True))
dlg.show(); app.processEvents()
dlg._request_capture(); app.processEvents()
print("[6] 点击截图选区后对话框自隐藏:", not dlg.isVisible(), "(expect True)")
print("[7] 请求信号发出:", captured_req["v"], "(expect True)")
dlg.set_template_image("tpl_new_001.png")
dlg.apply_to(step)
print("[8] 模板回写步骤:", step.params["image"], "(expect tpl_new_001.png)")
dlg.finish_template_capture(); app.processEvents()
print("[9] 截完对话框恢复:", dlg.isVisible(), "(expect True)")

# ---- 运行中左栏三行（运行进度第二行）----
ft.toggle_flow(cfg.flows[0].id)
def check():
    t = ft.list.item(0).text().split("\n")
    print("[10] 运行中左栏:", t, "| 行数:", len(t))
    ft.stop_all()
QTimer.singleShot(400, check)
QTimer.singleShot(1500, app.quit)
app.exec()

# ---- 渲染 ----
app.processEvents()
ft.grab().save(".zcode/flow_3line.png")
print("rendered, ALL DONE")
