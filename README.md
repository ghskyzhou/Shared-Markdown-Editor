# Shared Markdown Editor

一个轻量的在线多人实时协作 Markdown 编辑器，使用 Flask、Socket.IO 和 SQLite
构建。它支持多篇文档、Markdown/LaTeX 实时预览、独立文档协作房间、登录密钥、
双向滚动同步以及带视觉换行的行号。

## 功能

- 多篇 Markdown 文档的首页、新建、标题修改和最后编辑时间
- 基于 Socket.IO 的多人实时编辑
- 不同文档使用独立协作房间
- SQLite 本地持久化
- Markdown、代码高亮和 MathJax 公式预览
- 编辑区与预览区双向滚动同步
- 自动视觉换行、对应行号留白和当前行高亮
- 简单的访问密钥登录，Session 有效期为 30 天

## 环境要求

- Python 3.10 或更高版本

## 安装

Windows:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS 或 Linux 使用：

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

## 配置

复制环境变量示例文件 `.env.example`，重命名为 `.env`，然后编辑 `.env`：

```dotenv
MARKDOWN_EDITOR_PASSWORD=your-login-password
MARKDOWN_EDITOR_SECRET_KEY=your-long-random-session-secret
HOST=127.0.0.1
PORT=5000
FLASK_DEBUG=0
```

`MARKDOWN_EDITOR_SECRET_KEY` 应使用足够长的随机字符串。可以用 Python 生成：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 运行

```powershell
python main.py
```

然后访问：

```text
http://127.0.0.1:5000
```

## 数据

文档保存在项目目录下的 `documents.db` 中。

## 测试

```powershell
python -m unittest discover -s tests -v
```
