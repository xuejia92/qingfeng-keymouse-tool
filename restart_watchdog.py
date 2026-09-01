"""热键看门狗：启动 main.py，监听 Ctrl+R，按下则终止并返回 0（触发 bat 重启）。

配合 restart.bat 使用：
- 按 Ctrl+R → 终止当前 main.py 子进程，退出码 0 → bat 的 goto restart 生效
- main.py 自行退出（正常关闭）→ 退出码 1 → bat 结束

用 keyboard 库监听全局热键（项目 requirements.txt 已含，与 hotkey_manager 同库）。
"""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    p = subprocess.Popen([sys.executable, "main.py"])
    restarted = [False]

    def on_hotkey() -> None:
        restarted[0] = True
        try:
            p.terminate()                     # main.py 可能已被手动关掉
        except Exception:
            pass

    try:
        import keyboard
        keyboard.add_hotkey("ctrl+r", on_hotkey)
    except Exception:
        pass                                   # 热键不可用时不阻塞正常启动

    p.wait()
    return 0 if restarted[0] else 1            # 0=因 Ctrl+R 被杀，需重启


if __name__ == "__main__":
    sys.exit(main())
