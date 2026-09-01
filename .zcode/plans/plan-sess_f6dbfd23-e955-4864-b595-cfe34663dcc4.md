## 目标

新增「自动化流程」：把鼠标点击、键盘连按、找图点击（外加"延时等待"）封装为四种模块，新建流程后把模块**拖入**流程列表编排顺序，运行流程即依次执行；每个步骤参数独立可编辑，找图失败默认终止流程（可按步骤改为继续），支持整体循环轮数与流程独立启停热键。

## 数据模型（app/config.py）

```python
FlowStep: type(click/press/find/wait) + name + params(dict) + continue_on_fail(bool)
Flow:     id + name + steps[] + hotkey("") + loops(1, 0=无限)
```
- params 为各类型字段字典：click=按键/单击双击/间隔/位置/次数/时长；press=按键/间隔/次数/时长；find=模板图/置信度/点击方式/偏移/搜索超时/找图区域；wait=秒数
- 拖入时从主界面当前快照**复制**参数（独立修改互不影响）；防呆：执行时 count=0 且时长=0 的步骤按 1 次处理，避免流程卡死
- AppConfig.flows 列表，随 config.json 持久化，加载时校验字段

## 执行层（app/tasks.py 重构 + 新 app/flows.py）

1. 从 ClickTask/PressTask/FindTaskRunner 中抽出三个公共执行函数（签名：参数对象 + stop_event + progress 回调 → 结束原因），原任务类改为薄封装——**单任务功能行为完全不变**，流程复用同一套逻辑
2. `FlowRunner(QObject)`：独立线程按步骤顺序执行；信号 stepStarted/stepFinished/stepProgress/stateChanged 驱动 UI；整体循环 loops 轮；步骤失败（找图超时）且未勾选"失败继续" → 流程终止并报告是第几步失败；手动停止/紧急停止（Ctrl+Alt+X 已有的 stop_all 会一并停止所有流程）

## UI（新 app/ui/flow_tab.py + app/ui/flow_dialog.py）

主窗口新增第 4 个标签「自动化流程」：
- 左侧流程列表（名称·热键·步骤数，运行时显示"▶ 步骤 2/3"绿色状态）；工具栏：新建/编辑/删除/运行·停止选中
- 流程编辑器对话框：
  - 流程名、执行轮数（1~9999，0=无限）、流程启停热键（HotkeyEdit 录制，可选）
  - 模块面板：4 个可拖拽模块按钮（鼠标点击/键盘连按/找图点击/延时等待），用 QDrag 拖入步骤列表
  - 步骤列表 QListWidget：接受面板拖入（插入到松手位置）、列表内拖拽排序、删除；每步显示摘要（如"鼠标点击 · 左键 · 100ms × 10"）
  - 双击/选中步骤 → 按类型的参数编辑对话框（复用现有控件风格；找图步骤含模板下拉选择、框选区域、失败继续勾选框）
- 流程热键纳入现有热键调度（重复冲突检测同样生效）；托盘"全部停止"天然覆盖流程

## 实施步骤

1. config.py：FlowStep/Flow/AppConfig.flows + 校验
2. tasks.py：抽公共执行函数，原三类任务改薄封装（回归验证三大页面行为不变）
3. flows.py：FlowRunner
4. flow_dialog.py + flow_tab.py：编辑器与标签页（拖拽入列、排序、参数编辑）
5. main_window.py：挂标签、流程热键注册、stop_all 扩展
6. README 更新（含"流程中无限步骤按 1 次执行"的防呆说明）
7. 验证：离屏单测（配置往返、公共执行函数、FlowRunner 顺序/失败停止/循环/手动停止）+ 实机重启（热键注册日志、UI 截图）+ 重新打包 exe

## 交付物

带「自动化流程」标签页的完整程序，拖拽编排 + 顺序执行 + 独立热键/循环/失败策略，实测验证后更新 exe。