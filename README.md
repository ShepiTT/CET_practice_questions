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

将你的英语四级真题 PDF 文件放入项目根目录下的 `data` 文件夹中。建议文件命名格式为 `YYYY-MM-CET4-X.pdf`（如 `2024-06-CET4-1.pdf`）。

### 4. 运行系统

```bash
python app.py
```

启动后，访问浏览器：`http://127.0.0.1:5000` 即可开始使用。

## 📂 项目结构

```text
英语刷题系统/
├── data/               # 存放 PDF 真题文件
├── instance/           # 自动生成的 SQLite 数据库文件
├── templates/          # HTML 模板文件 (Bootstrap 5)
├── app.py              # Flask 主程序与路由逻辑
├── models.py           # 数据库模型定义
├── parser.py           # PDF 解析核心逻辑
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

希望这个工具能帮助你高效备考，顺利通过英语四级考试！🚀
