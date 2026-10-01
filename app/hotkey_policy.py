"""热键冲突检测：集中配置里所有已分配热键，供各处录入控件实时校验。

MainWindow 启动时调用 set_config(cfg) 注入当前配置。cfg 对象之后是就地变更
（find_tasks/flows 列表增删、clicker/presser 字段替换），这里始终持有同一对象
引用，实时反映最新值。各热键录入处用 check(candidate, exclude_slot) 拿到冲突
归属名（None=无冲突），据此拒绝重复设置。

slot 约定（唯一标识一个热键槽位，编辑时用于排除自身，避免与旧值误判）：
  show_hide / stop_all / clicker / presser / middle_menu /
  find_task:<id> / flow:<id> / group_run:<分组名>
"""
from __future__ import annotations

from .config import AppConfig
from .hotkey_manager import HotkeyManager

_cfg: AppConfig | None = None


def set_config(cfg: AppConfig | None) -> None:
    """注入当前配置（None 可清空，测试用）。"""
    global _cfg
    _cfg = cfg


def collect_hotkeys(cfg: AppConfig) -> list[tuple[str, str, str]]:
    """收集配置里所有已分配热键，返回 [(slot, 归属名, 热键)]；空热键跳过。"""
    slots: list[tuple[str, str, str]] = []

    def add(slot: str, label: str, hotkey: str) -> None:
        hk = HotkeyManager.normalize(hotkey)
        if hk:
            slots.append((slot, label, hk))

    add("show_hide", "显示/隐藏窗口", cfg.show_hide_hotkey)
    add("stop_all", "紧急停止", cfg.stop_all_hotkey)
    for name, hk in (getattr(cfg, "group_hotkeys", {}) or {}).items():
        add(f"group_run:{name}", f"分组「{name}」", hk)
    add("clicker", "鼠标连点", cfg.clicker.hotkey)
    add("presser", "键盘连按", cfg.presser.hotkey)
    add("middle_menu", "中键菜单（快捷菜单）", cfg.middle_menu_hotkey)
    for t in cfg.find_tasks:
        add(f"find_task:{t.id}", f"找图任务「{t.name}」", t.hotkey)
    for f in cfg.flows:
        add(f"flow:{f.id}", f"流程「{f.name}」", f.hotkey)
    return slots


def find_hotkey_conflict(candidate: str, cfg: AppConfig,
                         exclude_slot: str = "") -> str | None:
    """候选热键是否与其它槽位冲突；返回冲突归属名，无冲突返回 None。"""
    hk = HotkeyManager.normalize(candidate)
    if not hk:
        return None
    for slot, label, hotkey in collect_hotkeys(cfg):
        if slot == exclude_slot:
            continue
        if hotkey == hk:
            return label
    return None


def check(candidate: str, exclude_slot: str = "") -> str | None:
    """热键校验，返回**可直接展示给用户**的拒绝原因（None=通过）。

    注意：Alt+字母组合**不再拦截**——热键引擎已改用 pynput 低级钩子
    （app/physical_hotkeys.py），能收到原始的物理 Alt+字母按键；
    且程序自己合成的按键不会再误触发热键。
    """
    if _cfg is None:
        return None
    owner = find_hotkey_conflict(candidate, _cfg, exclude_slot)
    if owner:
        return f"该热键已被「{owner}」占用，请换一个组合"
    return None
