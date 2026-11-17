# 智能文件整理助手 (file_organizer)

基于 **Tkinter** 的桌面文件整理工具。扫描目录、按规则 / AI 自动分类、查找重复文件、清理空目录，并为非模拟执行的操作记录撤销日志。移动和删除文件时不会覆盖同名目标，设计上"宁可多问一句，也不丢一个文件"。

```
扫描目录 → 规则/AI 分类 → 生成执行计划 → 用户确认 → 安全执行（防覆盖 + 软删除）→ 凭日志撤销
```

---

## 目录

- [功能概览](#功能概览)
- [环境要求](#环境要求)
- [安装](#安装)
- [运行](#运行)
- [界面功能说明](#界面功能说明)
- [项目结构](#项目结构)
- [核心模块参考](#核心模块参考)
- [安全设计](#安全设计)
- [AI 分类与配置](#ai-分类与配置)
- [配置文件说明](#配置文件说明)
- [测试与持续集成](#测试与持续集成)
- [代码风格](#代码风格)
- [常见问题](#常见问题)

---

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 目录扫描 | 遍历目录树，统计文件数量、大小，容错跳过无权限的文件/目录 |
| 智能分类 | 扩展名规则优先，规则无法识别时可选调用 AI（仅发送文件名，不发送内容） |
| 查重 | 先按文件大小分组，再计算 SHA-256 哈希，精确找出内容完全相同的文件 |
| 大/小文件统计 | 按大小排序展示文件，并计算每个文件占所选目录总大小的百分比 |
| 空目录清理 | 递归检测完全不含文件的目录（含嵌套空目录链） |
| 撤销历史 | 非模拟且有操作时写出撤销日志；日志及相关文件仍在时，可按批次还原 |
| 文件浏览 | 浏览目录并预览文本文件前 64 KiB 内容，只读不修改 |

扫描、哈希计算、AI 请求和文件执行通过后台线程处理，完成后在界面显示结果。

## 环境要求

- **建议 Python 3.9–3.12**（GitHub CI 已验证这些版本；3.8 和更新版本未纳入 CI 测试）
- **macOS 或 Windows**，并安装 Tkinter（GUI 依赖）：
  - **macOS**：从 [python.org](https://www.python.org/downloads/) 安装的 Python 通常包含 Tk；使用 Homebrew Python 时，如缺少 `_tkinter`，需安装与当前 Python 版本对应的 `python-tk` 组件。
  - **Windows**：从 [python.org](https://www.python.org/downloads/) 安装 Python，并在安装时保留 Tcl/Tk 组件。
- 全程使用 `pathlib`，不做手工字符串路径拼接
- 源码及程序写出的 JSON 文本使用 **UTF-8**；SQLite 缓存是数据库文件，原有用户文件不会被转换编码

## 安装

先在终端进入包含 `main.py` 的项目根目录，再执行以下命令。

**macOS：**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

**Windows（PowerShell）：**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

上述命令直接使用虚拟环境中的 Python，无需激活虚拟环境。

运行环境只依赖 `openai` 这一个第三方包（用于与 OpenAI 兼容接口通信；Claude 分类走内置的无依赖 HTTP 客户端）。

## 运行

在项目根目录执行（macOS）：

```bash
.venv/bin/python main.py
```

- Windows PowerShell 使用 `.\.venv\Scripts\python.exe main.py`；想避免弹出控制台窗口，可用 `.\.venv\Scripts\pythonw.exe main.py`。
- 首次启动会在 `~/.file_organizer/` 下创建配置目录（见下文）。

## 界面功能说明

| 页面 | 作用 |
| --- | --- |
| 文件浏览 | 浏览所选目录及子目录；选中文本文件可预览前 64 KiB 的 UTF-8 内容；Word/PDF/图片等需用系统应用打开。只读，不修改任何内容 |
| 整理归类 | 生成分类预览（规则 + 可选 AI），确认后按类别移动文件；不会覆盖同名目标 |
| 重复文件 | 先按大小筛选，再算 SHA-256；默认不自动勾选，可全选/取消/按"每组保留最新"快速选择；每组至少保留一份；执行前二次确认，默认移入程序回收区 |
| 大/小文件 | 按实际大小排序展示文件及其占比；可将手动选中的文件移入回收站，操作前需确认 |
| 空目录 | 递归查找完全不含文件的目录并批量清理 |
| 撤销历史 | 仅显示当前目标文件夹的记录；切换文件夹需重新选择；模拟运行（dry-run）不产生可撤销日志 |
| AI 缓存 | 查看/刷新/清空按文件名缓存的 AI 分类结果 |
| 设置 | 分类规则（扩展名 → 类别）与 AI 服务商配置 |

配置、AI 缓存与撤销日志统一保存在 `~/.file_organizer/`（`config.json`、`ai_cache.db`、`undo_logs/`），可通过菜单「工具 → 打开工作目录」直接查看。

## 项目结构

```text
.
├── main.py                     # 程序入口：从项目根目录启动 GUI
├── requirements.txt             # 运行时依赖（openai）
├── requirements-dev.txt         # 测试/静态检查依赖（pytest、flake8）
├── .flake8                      # 风格检查配置（行宽 120）
├── .github/workflows/ci.yml     # GitHub Actions：Python 3.9–3.12 上跑 flake8 + pytest
├── tests/                       # pytest 用例，覆盖扫描/分类/查重/清理/执行/撤销/GUI 辅助函数
└── file_organizer/
    ├── __init__.py
    ├── models.py                 # 数据模型：FileInfo / ScanResult / DuplicateGroup / PlannedAction
    ├── config.py                 # config.json 的加载、默认值合并、AI 服务商配置读写
    ├── scanner.py                 # 目录遍历（os.walk 封装），排除规则 + 权限错误容错
    ├── classifier.py              # 两层分类：本地扩展名规则 + AI 兜底（AIClassifier / Classifier）
    ├── ai_providers.py            # OpenAI / DeepSeek / Claude 三家服务商的请求与模型列表封装
    ├── storage.py                 # SQLite 实现的 AI 分类缓存（CacheStore），失败自动降级为内存字典
    ├── duplicate_checker.py       # 两阶段查重：按大小分组 → SHA-256 哈希比对
    ├── cleaner.py                 # 空目录检测（Cleaner.find_empty_dirs）与清理计划生成
    ├── executor.py                # 安全执行引擎：冲突改名、软删除（回收区）、撤销日志写入
    ├── undo.py                    # 读取撤销日志并原位还原，冲突时自动追加后缀
    ├── utils.py                   # 编码检测、人类可读大小、分块哈希、路径去重等工具函数
    └── gui.py                     # Tkinter 界面，将上述业务模块串联到各个功能页
```

## 核心模块参考

> 以下为各模块对外暴露的主要类型及职责，便于二次开发时快速定位代码。详细参数请见各模块的 docstring。

### `models.py` — 数据模型
- `FileInfo`：单个文件的元数据（路径、大小、修改时间、扩展名、分类及分类来源），`FileInfo.from_path()` 从磁盘读取并容错返回 `None`。
- `ScanResult`：一次目录扫描的结果集合，含跳过的目录/文件、排除的目录、总大小等统计属性。
- `DuplicateGroup`：一组内容相同的文件，提供 `recoverable_bytes` 等便捷属性。
- `PlannedAction`：一条可逆的执行计划（`move` / `delete` / `rmdir`），可序列化进撤销日志（`to_dict` / `from_dict`）。

### `scanner.py` — 目录扫描
- `Scanner(root, config=None, exclude_names=None, exclude_paths=None)`：按配置中的排除名单遍历目录；`scan()` 返回 `ScanResult`。遍历过程中遇到的 `PermissionError`／`OSError` 只记录、不中断。

### `classifier.py` — 分类引擎
- `Classifier`：入口类，先用扩展名规则分类，命中规则直接返回；未命中的文件交给 `AIClassifier`，无结果则归为 `Others`。
- `AIClassifier`：按 `batch_size`（≤20）将待分类文件名打包，查本地 SQLite 缓存，缓存未命中才真正发起 AI 请求；只发送**文件名**，不发送文件内容。
- `ClassificationReport`：统计一次分类中有多少文件来自规则 / 缓存 / AI / 仍未解析。

### `ai_providers.py` — AI 服务商适配
- 统一封装 **OpenAI**、**DeepSeek**（OpenAI 兼容）与 **Claude**（Anthropic Messages API，独立实现，不依赖 `openai` SDK）三家服务商的请求、模型列表获取与连接测试（`TestResult`）。
- `PROVIDER_INFO` 保存各服务商的默认模型、默认 Base URL、默认环境变量名等静态信息。

### `storage.py` — 分类缓存
- `CacheStore(db_path=None)`：SQLite 持久化的 key-value 缓存；打开失败（只读卷、文件损坏等）时自动降级为纯内存字典，保证主流程不受影响。

### `duplicate_checker.py` — 查重
- `DuplicateChecker(files, chunk_size=1MB, include_empty=False)`：第一阶段按文件大小分组（零成本），第二阶段只对体积相同的候选文件计算 SHA-256；`find()` 返回按"可释放空间"排序的 `DuplicateGroup` 列表。

### `cleaner.py` — 空目录清理
- `Cleaner.find_empty_dirs(root, protected=None, exclude_names=None)`：自底向上判断，子目录全部可清理时父目录也视为可清理，一次遍历处理任意深度的嵌套空目录链。
- `Cleaner.plan_dir_removal(dirs, reason=...)`：转换为 `rmdir` 类型的 `PlannedAction` 列表。

### `executor.py` — 安全执行引擎
- `Executor(trash_dir=None, log_dir=None, config=None, dry_run=False)`：
  - `resolve_conflict(destination)`：目标已存在时依次尝试 `名称_时间戳.ext` → `名称_时间戳_1.ext`，**绝不覆盖**。
  - `execute(actions)`：顺序执行 `move` / `delete` / `rmdir`，单个文件失败不会中断整批；返回 `ExecutionReport`（成功/失败/跳过数量、移动与回收字节数、撤销日志路径）。
  - 删除操作实际是把文件移入 `<trash_dir>/<批次ID>/<原盘符>/<原相对路径>`（软删除），路径过长时用哈希前缀缩短，仍保留原扩展名。
  - 非 dry-run 且有操作时尝试写出 `undo_log_<批次>.json`；若日志写入失败会抛出异常提示手动核查。

### `undo.py` — 撤销
- `UndoManager(log_dir)`：`list_logs()` 列出可用的撤销日志（最新在前）；`restore(log)` 原位还原，若原位置已被占用则自动追加 `_restored` / `_restored_1` 后缀，不会覆盖。

### `utils.py` — 通用工具
- 人类可读文件大小 (`human_readable_size`)、时间戳格式化、扩展名规范化、分块 SHA-256 哈希 (`chunked_hash`)、JSON 读写、唯一路径生成 (`unique_path`) 等，供其余模块共用。

### `gui.py` — 界面
- 直接复用以上所有业务模块（`Scanner` / `Classifier` / `DuplicateChecker` / `Cleaner` / `Executor` / `UndoManager`），主要耗时操作放入后台线程，通过队列与主线程通信刷新界面。
- `main()` 为 GUI 的程序入口，被 `main.py` 调用。

## 安全设计

| 机制 | 说明 |
| --- | --- |
| 绝不覆盖 | 目标同名时依次尝试 `名称_20231027.pdf` → `名称_20231027_1.pdf` |
| 危险确认 | 删除文件或目录前要求用户确认 |
| 软删除 | "删除"实际是移入 `<所选目录>/.file_organizer_trash/<批次>/<原盘符>/<原相对路径>` |
| 可撤销 | 非模拟且有操作时写出 `undo_log_<批次>.json`；日志及相关文件仍在时，可在"撤销历史"中还原 |
| 还原不覆盖 | 原位置已存在同名文件时，自动还原为 `名称_restored_1.ext` |
| 容错 | 遇到常见的文件系统错误时记录并跳过相关项，尽量继续处理其余文件或目录 |

## AI 分类与配置

AI 仅在"整理归类 → 生成预览"中参与分类：**先**按扩展名规则分类；规则未命中的文件先查本地 SQLite 缓存，缓存没有结果时才把**文件名**（不含文件内容）发送给 AI。未配置有效 API Key 或请求失败时，仍无法识别的文件归入 `Others`。生成预览不会移动文件，但可能产生 AI 请求费用。

"设置 → AI 分类"支持在 **Claude（Anthropic）/ ChatGPT（OpenAI）/ DeepSeek** 三个服务商之间切换，各服务商独立保存模型、Base URL、API Key，互不覆盖。Base URL 留空使用官方地址，也可填自定义地址（私有部署/代理）。模型下拉框支持手动输入，也可点击"获取模型列表"向服务商拉取当前可用模型；接口不可用时回退到内置的常用模型列表。

### API Key 查找顺序

程序按以下顺序查找当前所选服务商的密钥，**第一个非空值生效**：

1. 设置中填写的该服务商 API Key（明文保存在本机 `config.json` 的 `ai.providers.<provider>.api_key`）
2. `ai.providers.<provider>.api_key_env` 指定名字的环境变量（默认分别为 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`）
3. 通用环境变量 `FILE_ORGANIZER_API_KEY`（任意服务商均可生效）
4. 常见厂商环境变量：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`AZURE_OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`DASHSCOPE_API_KEY`、`ZHIPUAI_API_KEY`、`SILICONFLOW_API_KEY`、`OPENROUTER_API_KEY`、`GROQ_API_KEY`、`TOGETHER_API_KEY`、`MISTRAL_API_KEY`

### AI 设置项一览

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| 启用 AI 补充分类 (`enabled`) | 开 | 分类页复选框的默认状态，可临时取消/勾选 |
| AI 服务商 (`provider`) | `openai` | 当前生效服务商：`claude` / `openai` / `deepseek` |
| 模型（每服务商独立的 `model`） | 各服务商官方默认模型 | 可手动填写或从"获取模型列表"中选择 |
| Base URL（每服务商独立的 `base_url`） | 空＝官方地址 | 可填自定义地址 |
| API Key（每服务商独立的 `api_key`） | 空 | 接口密钥，输入框以 `•` 遮挡显示 |
| API Key 环境变量名 (`api_key_env`) | 见上文默认值 | 固定读取某个自定义变量名 |
| 每批文件数 (`batch_size`) | 20 | 单次请求最多发送多少个文件名，硬上限 1–20 |
| 最多送 AI 的文件数 (`max_files`) | 400 | 单次最多请求多少个未缓存文件名；0 表示不限制 |
| 超时 (`timeout`) | 30 秒 | 单次 API 请求超时时间 |

点击"测试连接"会用当前填写的 Key / Base URL / 模型发送一次最小请求，用于验证配置是否可用：成功会显示服务商、实际测试的模型与耗时；失败会给出可读的原因（Key 无效、无模型权限、余额不足、超时、地址错误等）。测试期间按钮会置灰并提示"正在测试…"，避免重复点击；该操作本身会消耗少量 API 额度。

AI 结果按文件名（含后缀）缓存在 `~/.file_organizer/ai_cache.db`，同名文件可复用，没有 API Key 时也能读取已保存的结果。"AI 缓存"页可查看、刷新或清空缓存；由于缓存按文件名复用，可能包含来自不同目标文件夹的记录。

## 配置文件说明

首次运行会在 `~/.file_organizer/config.json` 生成默认配置，核心字段：

```jsonc
{
  "version": 1,
  "categories": ["Images", "Videos", "Audio", "Documents", "Spreadsheets",
                 "Presentations", "Archives", "Code", "Data",
                 "Executables", "Fonts", "Design", "Others"],
  "extension_map": { ".jpg": "Images", ".pdf": "Documents", "...": "..." },
  "ai": { "enabled": true, "provider": "openai", "batch_size": 20,
          "max_files": 400, "timeout": 30, "providers": { "...": "..." } },
  "scan": { "exclude_names": [".git", "__pycache__", "node_modules", "..."],
            "follow_symlinks": false },
  "clean": { "top_large_files": 10 },
  "safety": { "trash_dir_name": ".file_organizer_trash",
              "require_delete_confirmation": true }
}
```

配置文件为旧版本或缺失字段时，程序会与默认值递归合并，不会因为升级而损坏；`extension_map` 可自由增删扩展名与类别的对应关系。

## 测试与持续集成

在项目根目录运行。若已按“安装”一节创建过 `.venv`，可以跳过对应代码块的第一行。

**macOS：**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -v
.venv/bin/python -m flake8 file_organizer main.py tests
```

**Windows（PowerShell）：**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m flake8 file_organizer main.py tests
```

两种系统都直接使用虚拟环境中的 Python；macOS 不要用 `--break-system-packages` 安装到系统 Python。

测试全部在临时目录中运行，不会读写 `~/.file_organizer/`。`.github/workflows/ci.yml` 在每次 push 或 Pull Request 时，于 Python **3.9 – 3.12** 上运行 `flake8` 与 `pytest`。

`tests/` 覆盖范围：`scanner`（扫描与排除规则）、`classifier` / `test_ai_classification`（规则与 AI 分类）、`duplicate_checker`（查重）、`cleaner`（空目录）、`executor_edges` / `executor_undo`（执行与撤销边界情况）、`storage`（缓存降级）、`config`、`utils_models`、`gui_helpers` / `gui_workflows`（界面辅助函数与工作流）。

如需取得语句覆盖率（不参与 CI，也不设通过门槛）：

```bash
.venv/bin/python -m pip install coverage
.venv/bin/python -m coverage run --source=file_organizer -m pytest -q
.venv/bin/python -m coverage report --skip-covered
```

报告中的 `TOTAL` 是测试执行到的可执行语句比例，不代表所有功能都正确。

在 Windows PowerShell 中运行覆盖率命令时，将上述 `.venv/bin/python` 替换为 `.\.venv\Scripts\python.exe`。

## 代码风格

- 由 `flake8` 检查常见代码风格问题和潜在错误，配置见 `.flake8`；行宽上限设为 **120 列**。当前配置不检查文档字符串是否完整。
- 主要业务模块使用 `from __future__ import annotations` 和类型注解；数据模型使用 `dataclass`。
- 主要类与核心函数配有英文 docstring，说明用途和部分边界行为。

## 常见问题

**启动后提示缺少 `_tkinter`？**
按上文“环境要求”安装 Tkinter。macOS 运行 `.venv/bin/python -m tkinter`，Windows PowerShell 运行 `.\.venv\Scripts\python.exe -m tkinter`；能够弹出测试窗口即表示安装成功。

**AI 分类一直落到 `Others`？**
依次检查：是否已在"设置 → AI 分类"中启用并选对服务商；API Key 是否按上文"查找顺序"配置正确；"测试连接"是否成功；网络/代理是否可达对应服务商的 Base URL。

**误删/误移动了文件怎么办？**
打开"撤销历史"页，找到对应批次执行"撤销"；若已经清空回收区或撤销日志被手动删除，则无法恢复——这也是为什么执行危险操作前务必确认预览内容。

**升级后配置是否需要手动迁移？**
不需要。`config.json` 在每次加载时都会与内置默认值递归合并，缺失字段自动补全，旧字段也会被保留。
