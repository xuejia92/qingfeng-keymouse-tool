import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

import json
import tempfile

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

# 防护：把配置路径指到临时文件，避免测试覆盖真实 config.json
import app.config as _cfgmod
_cfgmod.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "qf_test_config.json")

from app.config import AppConfig, Flow, FlowStep
from app.hotkey_manager import HotkeyManager
from app.ui.main_window import MainWindow

# ---- 1. 旧配置迁移 ----
data = {"show_hide_hotkey": "ctrl+alt+q", "stop_all_hotkey": "ctrl+alt+x"}
json.dump(data, open(_cfgmod.CONFIG_PATH, "w", encoding="utf-8"))
cfg = AppConfig.load()
print("[1] 旧默认迁移 -> show:", cfg.show_hotkey, "hide:", cfg.hide_hotkey,
      "(expect shift+f1 / shift+f2)")

data["show_hide_hotkey"] = "ctrl+alt+5"  # 用户自定义过的旧值应保留
json.dump(data, open(_cfgmod.CONFIG_PATH, "w", encoding="utf-8"))
cfg = AppConfig.load()
print("[2] 旧自定义值迁移 -> show:", cfg.show_hotkey, "(expect ctrl+alt+5)")

# ---- 2. 主窗口（单例，与生产一致）----
app = QApplication([])
cfg.flows = [Flow(name="流程甲", steps=[FlowStep(type="wait", params={"seconds": 30}),
                                        FlowStep(type="press", params={"keys": "space", "count": 3})]),
             Flow(name="流程乙", loops=0)]
win = MainWindow(cfg, HotkeyManager())
win.show()
app.processEvents()
print("[3] 状态栏提示:", win.status_hint.text())
print("[4] 调度表含 show/hide:", "shift+f1" in win._dispatch, "shift+f2" in win._dispatch)
print("[5] settings values:", win.settings_tab.values())

# ---- 3. 流程页左右两栏 ----
ft = win.flow_tab
print("[6] 左栏流程数:", ft.list.count(), "(expect 2)")
print("[7] 默认选中右栏标题:", ft.right_title.text())
ft.list.setCurrentRow(0)
app.processEvents()
print("[8] 选中后右栏标题:", ft.right_title.text())
print("[9] 右栏模块数:", ft.step_list.count(), "(expect 2)")
print("[10] 右栏首行:", ft.step_list.item(0).text())

# ---- 4. 运行高亮 + 停止 + 干净退出 ----
ft.toggle_flow(cfg.flows[0].id)


def probe():
    print("[11] 运行中右栏当前步:", ft.step_list.item(0).text(),
          "| 左栏:", ft.list.item(0).text())
    ft.stop_all()


def finish():
    print("[12] 停止后左栏文本:", ft.list.item(0).text())
    print("ALL DONE")
    app.quit()


QTimer.singleShot(300, probe)
QTimer.singleShot(600, finish)
QTimer.singleShot(8000, app.quit)
app.exec()
