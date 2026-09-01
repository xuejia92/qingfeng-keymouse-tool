# 项目长期记忆

## 发布渠道（2026-09-01 起）
- **GitHub Releases 为主发布渠道**：仓库 **`xuejia92/qingfeng-keymouse-tool`**（2026-09-01 由 xuejia92/- 改名，旧链接自动 301）。
- 原因：Gitee release 单文件上限 100MB，exe 145MB 传不上去；GitHub 上限 2GB。
- 网络：GitHub 需走本地代理 `http://127.0.0.1:7897`（Clash）；环境变量中旧的 14755 端口已失效。gh 命令前加 `HTTPS_PROXY=http://127.0.0.1:7897`。
- **gh CLI 中文文件名 bug**：`gh release upload` 在 Windows 下会把中文资产名改成 default.exe，不可用；发布统一走 `dist_tools\打包并发布到github.exe`（publish_tool.py，tk 桌面工具：填版本号 → 自动打包+发 Release，资产名用 ASCII `QingFeng_KeyMouse_Tool.exe`）。
- 在线更新（updater.py）：GitHub API 优先（release 显示名做版本号，tag 是中文「键鼠自动化」不参与比对）→ Gitee 兜底 → Gitee manifest 最后兜底。
- 发布注意：PyInstaller --icon 必须用绝对路径；发布工具打包后 sys.executable 不是 python，靠 find_python() 探测 3.12。

## 打包方案（2026-09-01 起）
- **PyInstaller**（6.15.0，约 1 分钟出单文件，145.3MB 含 OCR）；此前用过 Nuitka（101MB 但 7-25 分钟需 C 编译器、曾遇 ccache 并发写冲突）。
- build.py 要点：hidden-imports（pynput/mss 平台后端）、excludes（transformers/torch/tensorflow/keras/cpuinfo/py3nvml）、add-data（assets、DrissionPage configs.ini/suffixes.dat、RapidOCR config.yaml+3 模型）。
- 测试需用 3.12 解释器跑（PATH 上 managed 3.13 无依赖）；test_instance_lock 有 5 个预存 error 与代码无关。
