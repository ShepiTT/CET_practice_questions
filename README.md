# CET-4 英语刷题系统 (CET-4 English Practice System)

这是一个基于 Python Flask 框架开发的英语四级（CET-4）真题刷题系统。它能够自动从 PDF 格式的真题卷子中提取题目，并提供一个现代化的 Web 界面供用户进行练习、查看解析以及管理错题。

## 🌟 核心功能

- **自动化 PDF 解析**: 利用 `pdfplumber` 和强大的正则表达式，自动从 `data` 文件夹下的 PDF 真题中提取听力、选词填空、段落匹配和仔细阅读题目。
- **智能题目分组**: 按照考试原有的逻辑对题目进行分组（如：Questions 16-18 基于同一篇听力原文），确保练习时的上下文连贯性。
- **多种刷题模式**:
  - **按卷刷题**: 完整体验某一年份的真题。
  - **随机模式**: 从所有题库中随机抽取题目组，适合碎片化练习。
- **后台管理系统**:
  - **一键导入答案**: 支持直接粘贴包含“详解”、“涉及知识点”等复杂格式的答案文本，系统会自动识别并匹配题号。
  - **手动校对**: 可以在后台手动设置每一题的标准答案。
- **错题集管理**:
  - **按组展示**: 错题不仅仅显示单题，还会带上完整的原文和同组的其他题目，方便复习。
  - **二刷功能**: 支持在错题集中一键重练，实时反馈对错。
- **听力音频**:
  - 每套卷子的 MP3 放在 `data/audio/` 下即自动识别，页面底部一个播放器，每组听力题一个"播放本组"按钮。
  - 第一遍听的时候点"标记本组起点"，之后直接从该组开始播放。
  - 没有音频的卷子，听力题不会进入随机模式（避免靠猜污染统计）。
- **交互优化**:
  - **显示/隐藏答案**: 支持在练习过程中随时切换答案和详解的可见性。
  - **词汇库展示**: 选词填空（Section A）上方独立显示 Vocabulary Bank，模拟真实考试体验。

## 🛠️ 技术栈

- **后端**: [Flask](https://flask.palletsprojects.com/) (Python Web 框架)
- **数据库**: [SQLite](https://www.sqlite.org/) + [SQLAlchemy](https://www.sqlalchemy.org/) (ORM)
- **PDF 处理**: [pdfplumber](https://github.com/jsvine/pdfplumber) + Regex
- **前端**: [Bootstrap 5](https://getbootstrap.com/) (响应式布局) + Jinja2 (模板引擎)

## 🚀 快速开始

### 1. 环境准备

确保你的电脑上已安装 Python 3.8+。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 准备数据

一条命令拉取全部真题、答案解析和听力音频（来源：[CET通](https://www.cettong.cn) 的公开仓库，免费、仅供个人学习）：

```bash
python fetch_library.py
```

文件会按项目命名放好：试卷 `data/YYYY-MM-CET4-X.pdf`、答案 `data/answers/`、听力 `data/audio/YYYY-MM-CET4-X.mp3`。以后网站更新了再跑一次即可，已有文件不会被覆盖。也可以手动把 PDF 放进 `data/`、MP3 放进 `data/audio/`，文件名对上就能识别。

### 4. 运行系统

```bash
python app.py
```

启动后，访问浏览器：`http://127.0.0.1:5000` 即可开始使用。

## 📂 项目结构

```text
英语刷题系统/
├── data/               # 存放 PDF 真题文件
│   ├── audio/          # 听力 MP3（fetch_library.py 拉取）
│   └── answers/        # 答案解析 PDF（fetch_library.py 拉取）
├── instance/           # 自动生成的 SQLite 数据库文件
├── templates/          # HTML 模板文件 (Bootstrap 5)
├── tests/              # pytest 测试（python -m pytest tests）
├── app.py              # Flask 主程序与路由逻辑
├── models.py           # 数据库模型定义
├── parser.py           # PDF 解析核心逻辑
├── fetch_library.py    # 同步真题 / 答案 / 音频
├── repair_db.py        # 修复旧数据库（去重选项、清理假记录），可重复运行
├── requirements.txt    # 项目依赖清单
└── README.md           # 项目介绍文档
```

## 📝 导入答案说明

在后台管理页面的“一键导入”功能中，系统支持多种识别模式，包括：
- `题号：第 1 题 答案：D 详解：...`
- `26 Blank 26... 答案：N (unique) ...`
- `Q1 - 第1页 ... 正确答案: B ...`

系统会自动忽略干扰文字，精准提取答案字母并将后续内容存入详解。

---

## 数据核对与听力定位

维护工具依赖单独列在 `requirements-data.txt`。以下命令默认只生成审计报告；加 `--apply` 才会备份数据库并写入。答案补全仅填空缺，已有答案冲突保留在报告中，不自动覆盖。

```bash
python fetch_reference_keys.py --all
python repair_reference_text.py
python complete_answer_keys.py
python align_listening.py
```

参考答案和原文来自懒笔记公开试卷页，先核对题干、选项和段落身份。原始页面快照保存在 `.cache/reference/`，来源 URL、哈希及变更记录保存在 `instance/*-*.json`。`repair_reference_text.py` 的文本变更需先检查报告，确认试卷一致后再应用。

`transcribe_listening.py` 生成本地录音的逐词时间戳，`align_listening.py` 将原文开头与这些时间戳匹配，不直接套用网页音频的时间。`refine_listening.py <审计文件>` 可复查漏识别的开头；不确定的定位保持为空。ASR 模型目录为 `.cache/models/base.en/`（`Systran/faster-whisper-base.en`）。

本机数据任务使用明确的 CPU 亲和性和低优先级，至少保留 4 个完整物理核心。其他机器须按其实际拓扑选择 CPU 编号；无法确认保留核心时拒绝启动。
