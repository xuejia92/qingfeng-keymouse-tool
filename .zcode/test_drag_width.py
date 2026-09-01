import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer
from PySide6.QtGui import QDropEvent, QEnterEvent
from PySide6.QtWidgets import QApplication

from app.config import AppConfig, Flow
from app.ui.flow_dialog import FlowDialog, MIME_TYPE, StepList

app = QApplication([])
cfg = AppConfig()
flow = Flow(name="拖拽测试")
dlg = FlowDialog(flow, cfg.clicker, cfg.presser)
dlg.show()
app.processEvents()
lst = dlg.step_list


def drop_mime(step_type: str, row_hint_y: int):
    """模拟模块面板拖放到列表 y 位置。"""
    mime = QMimeData()
    mime.setData(MIME_TYPE, step_type.encode())
    ev = QDropEvent(QPoint(10, row_hint_y), Qt.MoveAction, mime,
                    Qt.LeftButton, Qt.NoModifier)
    lst.dropEvent(ev)


# ---- 1. 事件级拖入：三个模块依次放入 ----
drop_mime("click", 15)
drop_mime("press", 40)
drop_mime("wait", 65)
app.processEvents()
print("[1] 拖入3步:", [s.type for s in dlg._steps], "(expect click/press/wait)")
print("[2] 列表行数:", lst.count(), "(expect 3)")

# ---- 3. dragEnter/dragMove 接受自定义 MIME ----
mime = QMimeData()
mime.setData(MIME_TYPE, b"find")

from PySide6.QtGui import QDragEnterEvent
enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime,
                        Qt.LeftButton, Qt.NoModifier)
lst.dragEnterEvent(enter)
print("[3] dragEnter 接受:", enter.isAccepted(), "(expect True)")

# ---- 4. 内部排序（orderChanged → _sync_order 路径）----
order_before = [s.type for s in dlg._steps]
it = lst.takeItem(0)
lst.insertItem(2, it)
lst.setCurrentRow(2)
lst.orderChanged.emit()
app.processEvents()
print("[4] 排序后:", [s.type for s in dlg._steps], "(expect press/wait/click)")
print("[5] 列表行序:", [lst.item(i).text().split(".")[0] for i in range(lst.count())],
      "(expect ['1','2','3'])")

# ---- 6. FlowTab 左栏 20% 宽 ----
from PySide6.QtWidgets import QSplitter
from app.hotkey_manager import HotkeyManager
from app.ui.main_window import MainWindow
cfg.flows = [Flow(name="f1"), Flow(name="f2")]
win = MainWindow(cfg, HotkeyManager())
win.resize(1000, 600)
win.show()
app.processEvents()
# 切到自动化流程标签，触发 showEvent 的比例应用
win.centralWidget().setCurrentWidget(win.flow_tab)
app.processEvents()
sp = None
for c in win.flow_tab.findChildren(QSplitter):
    sp = c
sizes = sp.sizes()
total = sum(sizes)
print("[6] splitter 比例: %.0f%% / %.0f%%" % (sizes[0] / total * 100, sizes[1] / total * 100),
      "(expect 20% / 80%)")
app.quit()
print("ALL DONE")
