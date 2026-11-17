# 智能文件整理助手（file_organizer）

基于 Tkinter 图形界面的文件整理工具：扫描、分类、查重、空目录清理与撤销。移动文件不覆盖同名目标，重复文件先移入程序回收区。

## 兼容性

* Python **3.8+**（已规避 PEP 604 `str | None`、PEP 585 `list[str]`、`dataclass(slots=True)`、`Path.is_relative_to()` 等 3.9/3.10+ 语法）
* 全程 `pathlib`，不做字符串拼接
* 所有源码与输出文件均为 **UTF-8**；`requirements.txt` 为纯 ASCII

## 安装

```bash
cd /path/to/文件管理器_代码
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell 激活命令：`.venv\Scripts\Activate.ps1`。

## 启动

从项目根目录运行：

```bash
python main.py
```

Windows 下想不弹出命令行窗口，可使用 `pythonw main.py`。

GUI 使用 Tkinter。在 Homebrew Python 3.14 下若提示缺少 `_tkinter`，先运行 `brew install python-tk@3.14`，再用 `python -m tkinter` 检查是否能打开测试窗口。

## 功能

界面提供文件浏览、扫描、分类预览与选择、查重、空目录、大/小文件、撤销、分类规则和设置。

* “文件浏览”页可查看所选目录及其子目录，选中文件可预览前 64 KiB 的 UTF-8 文本；Word、PDF、图片等非文本文件需用系统应用打开。该页只读取文件，不修改内容。
* “重复文件”页先按大小筛选，再计算文件内容的 SHA-256。扫描后默认不选择文件；可逐个勾选、全选、取消选择，或用“每组保留最新，选其余副本”快速选择。每组至少要留一份，处理前再次核对内容并要求确认，默认移入程序回收站。
* 大/小文件页显示每个文件的实际大小和它占所选目录文件总大小的百分比；也可将手动选中的文件移入回收站，操作前会要求确认。
* “撤销历史”只显示当前目标文件夹的记录，切换文件夹后需重新选择记录；模拟操作不会生成可撤销日志。

配置、AI 缓存和撤销日志保存在 `~/.file_organizer/`（`config.json`、`ai_cache.db`、`undo_logs/`），可通过“工具 → 打开工作目录”查看。

## 目录结构

```
├── main.py                   # 项目入口
├── tests/                    # pytest 自动化测试（扫描/分类/查重/撤销）
├── .github/workflows/ci.yml  # GitHub Actions：flake8 + pytest
├── .flake8                   # 风格检查配置
├── requirements.txt
└── file_organizer/
    ├── __init__.py
    ├── gui.py               # Tkinter 图形界面与文件浏览
    ├── models.py            # FileInfo / ScanResult / PlannedAction 等数据模型
    ├── config.py            # config.json 加载、校验与默认值
    ├── storage.py           # SQLite AI 缓存（失败自动降级为内存字典）
    ├── scanner.py           # os.walk 遍历 + PermissionError 容错
    ├── classifier.py        # 双层分类：本地规则 + AI 兜底
    ├── duplicate_checker.py # 大小分组 → SHA-256 查重
    ├── cleaner.py           # 空目录检测 + 大文件统计
    ├── executor.py          # 冲突改名 / 软删除 / undo 日志
    ├── undo.py              # 读取 undo_log 回滚，冲突自动改名
    └── utils.py             # 编码、格式化、哈希、路径工具
```

## 安全设计

| 机制 | 说明 |
| --- | --- |
| 绝不覆盖 | 目标同名时依次尝试 `名称_20231027.pdf` → `名称_20231027_1.pdf` |
| 危险确认 | 删除文件或目录前要求确认 |
| 软删除 | 删除实际是移入 `<所选目录>/.file_organizer_trash/<批次>/<原盘符>/<原相对路径>` |
| 后悔药 | 每次执行写出 `undo_log_<时间戳>.json`，可在“撤销历史”中原位还原 |
| 还原不覆盖 | 原位置已存在时自动还原为 `名称_restored_1.ext` |
| 容错 | `PermissionError` 只记录跳过，程序绝不中断 |

## AI 分类配置

AI 只在“整理归类 → 生成预览”中参与分类：先按扩展名规则分类；规则认不出的文件先查本地缓存，缓存没有结果时才发送**文件名**给 AI，不会发送文件内容。未配置有效 API Key 或请求失败时，仍无法识别的文件归入 `Others`。生成预览不移动文件，但可能产生 AI 请求和费用。

“设置 → AI 分类”可以在 **Claude（Anthropic）/ ChatGPT（OpenAI）/ DeepSeek** 三个服务商之间切换。每个服务商各自保存模型、Base URL、API Key，互不覆盖；切换服务商、保存设置都不会丢失其他服务商已填写的内容。Base URL 留空即使用该服务商的官方地址；也可以填自定义地址（私有部署、代理等）。

模型下拉框可以直接编辑手动填写模型 ID，也可以点击“获取模型列表”向服务商的官方接口请求当前可用的模型，成功后自动填充下拉候选；接口不可用或未配置 Key 时会退回到内置的（会持续维护的）常用模型列表，不影响继续手动填写。

程序按下面的顺序查找当前所选服务商的密钥，**第一个非空的值生效**：

1. 设置中填写的该服务商 API Key（保存在 `config.json` 的 `ai.providers.<provider>.api_key`，明文保存在本机）；
2. `ai.providers.<provider>.api_key_env` 指定名字的环境变量（默认按服务商分别是 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`）；
3. 通用环境变量 `FILE_ORGANIZER_API_KEY`，无论用哪家服务都有效；
4. 常见厂商的环境变量：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`AZURE_OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`DASHSCOPE_API_KEY`、`ZHIPUAI_API_KEY`、`SILICONFLOW_API_KEY`、`OPENROUTER_API_KEY`、`GROQ_API_KEY`、`TOGETHER_API_KEY`、`MISTRAL_API_KEY`。

“设置 → AI 分类”提供以下选项：

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| 启用 AI 补充分类（`enabled`） | 开 | 决定分类页复选框的默认状态；分类页可临时取消或勾选 |
| AI 服务商（`provider`） | `openai` | 当前生效的服务商：`claude` / `openai` / `deepseek` |
| 模型（每个服务商各自的 `model`） | 各服务商官方默认模型 | 请求使用的模型；可手动填写或“获取模型列表”后选择 |
| Base URL（每个服务商各自的 `base_url`） | 空＝官方地址 | 留空使用官方接口地址，可填自定义地址 |
| API Key（每个服务商各自的 `api_key`） | 空 | 接口密钥，输入框中以 `•` 遮挡显示 |
| API Key 环境变量名（每个服务商各自的 `api_key_env`） | 见上 | 固定读取某个自定义变量名 |
| 每批文件数（`batch_size`） | 20 | 一次请求最多发送多少个文件名，限制为 1–20 |
| 最多送 AI 的文件数（`max_files`） | 400 | 一次最多请求多少个未缓存的文件名；0 表示不限制 |
| 超时（`timeout`） | 30 秒 | 单次 API 请求的超时时间 |

点击“测试连接”会用当前填写的 Key / Base URL / 模型，向该服务商发送一次最小请求（几个 token），用来验证配置是否可用：成功会显示服务商、实际测试的模型和耗时；失败会给出易懂的原因（API Key 无效、没有模型权限、余额不足、请求超时、地址不正确等）。测试期间按钮会置灰并显示“正在测试…”，避免重复点击；测试本身会消耗少量 API 额度，按钮旁边有提示。

AI 结果按文件名（含后缀）缓存在本程序专用的 `~/.file_organizer/ai_cache.db`，同名文件可复用，没有 API Key 时也能读取已保存的结果。“AI 缓存”页列出文件名、分类和缓存时间，可刷新或清空本程序的缓存。缓存按文件名复用，因此可能包含不同目标文件夹的记录。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -v
flake8 file_organizer main.py tests
```

测试全部在临时目录中运行，不会读写 `~/.file_organizer/`。`.github/workflows/ci.yml` 在每次 push 或 Pull Request 时于 Python 3.9–3.12 上运行 flake8 和 pytest。

演示前如需取得语句覆盖率，可在本地单独运行（不参与 CI，也不设置通过门槛）：

```bash
python -m pip install coverage
python -m coverage run --source=file_organizer -m pytest -q
python -m coverage report --skip-covered
```

报告中的 `TOTAL` 是本次测试执行到的可执行语句比例；它不代表所有功能都正确。
