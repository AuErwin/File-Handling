# 智能文件整理助手（file_organizer）

基于命令行的文件整理工具：扫描 → 分类 → 查重 → 清理 → 执行 → 撤销，全流程可回溯、不覆盖、不硬删除。

## 兼容性

* Python **3.8+**（已规避 PEP 604 `str | None`、PEP 585 `list[str]`、`dataclass(slots=True)`、`Path.is_relative_to()` 等 3.9/3.10+ 语法）
* 全程 `pathlib`，不做字符串拼接
* 所有源码与输出文件均为 **UTF-8**；`requirements.txt` 为纯 ASCII
* Windows 下自动把 stdout/stderr 切到 UTF-8，避免 GBK 控制台打印 `█` 崩溃

## 安装

```bash
cd /path/to/文件管理器_代码
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell 激活命令：`.venv\Scripts\Activate.ps1`。使用 Python 3.8 或更新版本；旧 `myenv` 是本地生成的 Windows 环境，不适合跨平台复用。

配置、AI 缓存和撤销日志默认保存在 `~/.file_organizer/`。要将这三项一起放到其他目录，在子命令前使用 `--work-dir`：

```bash
python -m file_organizer --work-dir "/path/to/organizer_data" cache
```

程序会在该目录下使用 `config.json`、`ai_cache.db` 和 `undo_logs/`。如需单独指定配置文件，使用 `--config "配置文件路径"`；单独指定日志目录则使用 `--log-dir "日志目录"`。`--work-dir` 不会搬迁原有数据，切换前请自行复制需要保留的配置、缓存或日志。


## 常用功能

```bash
# 扫描目录，输出文件数、总大小、扩展名分布
python -m file_organizer scan "D:/Downloads"

# 预览分类目标、重复文件和空目录（不移动文件）
python -m file_organizer report "D:/Downloads" --dest "D:/Sorted"

# 确认报告后执行分类整理
python -m file_organizer organize "D:/Downloads" --dest "D:/Sorted"

# 查重：按文件内容查找重复项，逐组选择保留哪份
python -m file_organizer dedupe "D:/Downloads"

# 查找并清理空目录
python -m file_organizer clean "D:/Downloads"

# 只显示最大的 10 个文件，不删除文件
python -m file_organizer largest "D:/Downloads" --top 10

# 只显示最小的 10 个文件，不删除文件
python -m file_organizer smallest "D:/Downloads" --top 10

# 撤销：列出日志 / 回滚最近一次
python -m file_organizer undo --list
python -m file_organizer undo

# AI 缓存
python -m file_organizer cache
python -m file_organizer cache --clear
```

上面的 `D:/Downloads` 和 `D:/Sorted` 是示例路径；macOS/Linux 可换成 `~/Downloads` 和 `~/Sorted`。这些命令可独立运行。`scan` 只统计文件，不分类；`report` 汇总分类目标、重复文件和空目录，不移动或删除待整理文件；与 `organize` 一样，未知类型可能调用 AI 并写入 AI 缓存，加 `--no-ai` 可只用本地规则。报告中的可回收空间是处理重复文件并清空回收区后的估计值。确认报告后运行 `organize`，会把文件移入目标目录的类别文件夹，文件内容不变。查重确认后，重复文件会移到源目录的 `.file_organizer_trash`；`clean` 只处理空目录，`largest` 和 `smallest` 只显示文件大小排行。查重和清理的危险操作需要输入大写 `DELETE`。

| 常用选项 | 用途 |
| --- | --- |
| `organize --no-ai` | 只用扩展名规则分类，未知类型放入 `Others` |
| `report --no-ai` | 预览时只用扩展名规则，不调用 AI |
| `dedupe --dry-run`、`clean --dry-run` | 预览结果，不改动文件或目录 |
| `dedupe --keep-newest` | 每组重复文件自动保留最新的一份，仍需确认处理 |
| `undo --log "日志路径"` | 指定要撤销的操作；不指定时使用最近一次日志 |
| `undo --dry-run` | 预览撤销，不还原文件 |

全局配置选项和其他较少使用的参数可运行 `python -m file_organizer --help` 查看；子命令选项可运行 `python -m file_organizer dedupe --help` 等查看。

## 目录结构

```
file_organizer/
├── file_organizer/
│   ├── __init__.py
│   ├── __main__.py          # python -m file_organizer 入口
│   ├── main.py              # argparse 子命令与编排
│   ├── models.py            # FileInfo / ScanResult / PlannedAction 等数据模型
│   ├── config.py            # config.json 加载、校验与默认值
│   ├── storage.py           # SQLite AI 缓存（失败自动降级为内存字典）
│   ├── scanner.py           # os.walk 生成器 + PermissionError 容错 + rich 进度条
│   ├── classifier.py        # 双层分类：本地规则 + AI 兜底
│   ├── duplicate_checker.py # 大小分组 → SHA-256 → 交互选择
│   ├── cleaner.py           # 空目录检测 + 大文件柱状图
│   ├── executor.py          # 冲突改名 / DELETE 二次确认 / 软删除 / undo 日志
│   ├── undo.py              # 读取 undo_log 回滚，冲突自动改名
│   └── utils.py             # 编码、格式化、哈希、路径工具
└── requirements.txt
```

## 安全设计

| 机制 | 说明 |
| --- | --- |
| 绝不覆盖 | 目标同名时依次尝试 `名称_20231027.pdf` → `名称_20231027_1.pdf` |
| 危险确认 | 删除/删目录前必须手动输入大写 `DELETE` |
| 软删除 | `delete` 实际是移入 `<root>/.file_organizer_trash/<批次>/<原盘符>/<原相对路径>` |
| 后悔药 | 每次执行写出 `undo_log_<时间戳>.json`，`undo` 命令原位还原 |
| 还原不覆盖 | 原位置已存在时自动还原为 `名称_restored_1.ext` |
| 容错 | `PermissionError` 只记录跳过，程序绝不中断 |

## AI 分类配置

默认关闭真实调用（无 Key 时自动降级为 `Others`）。启用方式（任选其一）：

```bash
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://your-compatible-endpoint/v1   # 可选
```

或在 `~/.file_organizer/config.json` 的 `ai` 段配置 `api_key` / `base_url` / `model` / `batch_size`（上限 20）。
只发送**文件名**，不发送文件内容；判定结果按「文件名+后缀」哈希写入 SQLite，同名不再重复消耗额度。
