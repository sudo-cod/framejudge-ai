# 帧证 FrameJudge AI

简体中文 | [English](README.en.md)

FrameJudge 是一款在本地运行的 AI 辅助视频比对与技术取证工具。它可以将一个原始视频与一个或多个可疑视频进行比对，分析画面和音频重合情况，并生成以技术证据为重点的中文 PDF 报告。

项目包含 FastAPI 后端和响应式浏览器界面，无需单独构建前端。

## 功能

- 在一个案件中，用一个原始视频同时比对多个可疑视频
- 使用本地 MobileNetV2/ONNX 模型检索候选相似帧
- 严格验证画面、几何变换、时间轴和音频重合情况
- 检测画中画、字幕、水印和遮挡证据
- 按红色、黄色和灰色进行风险分级
- 提供严格、标准、宽松及自定义阈值，并为每个案件保存配置快照
- 展示证据截图、匹配时间轴、审计信息和技术指标
- 为每个案件生成一份汇总 PDF 报告
- 支持案件历史记录、重命名、重新打开、下载报告和删除案件

## 隐私与数据存储

视频分析在运行 FrameJudge 的本地电脑上完成，视频帧不会发送给云端 AI 服务。

FrameJudge 会将上传的视频、临时帧、提取的音频、证据、分析结果和报告保存在自动生成的 `jobs/` 目录中。不再需要某个案件时，可在界面中删除它及其本地文件。

DeepSeek 报告润色功能是可选的。启用后，FrameJudge 只会向 DeepSeek 发送分析结果的精简文本摘要，不会发送视频或视频帧。未设置 DeepSeek API Key 时，系统会自动使用本地模板生成 PDF。

## 环境要求

- Windows 10/11，或较新的 macOS/Linux 系统
- Python 3.11（当前要求 `>=3.11,<3.12`）
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- 足够存放上传视频和临时分析文件的磁盘空间

项目已包含所需的 MobileNetV2 ONNX 模型。FFmpeg 由 `imageio-ffmpeg` 依赖提供。

## 安装并运行

### Windows PowerShell

```powershell
git clone https://github.com/sudo-cod/framejudge-ai.git
cd framejudge-ai

uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### macOS 或 Linux（Bash）

```bash
git clone https://github.com/sudo-cod/framejudge-ai.git
cd framejudge-ai

uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

然后在浏览器中打开 [http://127.0.0.1:8001](http://127.0.0.1:8001)。

第一次执行 `uv sync` 可能需要几分钟，因为需要安装视频、音频、OCR 和 ONNX 相关依赖。之后再次启动时，只需执行最后一条 `uv run uvicorn ...` 命令。

在终端中按 `Ctrl+C` 可停止应用。

## 使用方法

1. 如需调整判定标准，先打开“阈值设置”，选择严格、标准、宽松或自定义配置。新设置只会应用于之后创建的案件。
2. 返回“案件库”，选择“新建案件”。
3. 根据需要填写案件名称。
4. 选择一个原始视频。
5. 选择一个或多个可疑视频。
6. 点击“开始分析”，等待全部七个分析阶段完成。
7. 打开案件查看风险总览，并选择单个视频查看证据、时间轴、审计信息和技术详情。
8. 点击“下载 PDF 报告”保存案件汇总报告。

分析时间主要取决于视频时长、分辨率和 CPU 性能。大型案件可能需要较长时间，请勿在案件处理过程中停止服务器。

## 可选：使用 DeepSeek 生成报告文本

无需创建或编辑 `.env` 文件。直接在启动命令中设置 API Key 即可。

### macOS 或 Linux（Bash）

```bash
DEEPSEEK_API_KEY="你的-DeepSeek-API-Key" uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### Windows PowerShell

```powershell
$env:DEEPSEEK_API_KEY="你的-DeepSeek-API-Key"; uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Bash 内联变量只对本次启动的进程有效；PowerShell 变量会保留到当前终端关闭。两种方式都不会把密钥写入项目文件。不要将真实 API Key 提交到 GitHub，也不要在截图或日志中公开它。

`DEEPSEEK_MODEL` 为可选环境变量，默认值为 `deepseek-chat`。例如：

```bash
DEEPSEEK_API_KEY="你的-DeepSeek-API-Key" DEEPSEEK_MODEL="deepseek-chat" uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

未设置 API Key 时，FrameJudge 仍然可以正常分析视频，并使用本地模板生成报告。

## 项目结构

```text
app/
  main.py          FastAPI 路由、视频上传、案件和任务进度
  pipeline.py      视频分析流程编排
  scoring.py       阈值和风险分级
  pdf_report.py    案件汇总 PDF 生成
  static/          浏览器界面
  models/          内置 MobileNetV2 ONNX 模型
pyproject.toml     Python 依赖配置
uv.lock            可复现的依赖锁定文件
```

`jobs/`、`threshold-settings.json`、`.env`、缓存和生成的报告都是本地运行数据，不会提交到 GitHub。

## 局域网访问

如需让同一可信局域网内的设备访问，请使用：

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

然后在其他设备上打开 `http://<运行电脑的IP地址>:8001`。FrameJudge 当前没有用户认证功能，请勿将此端口直接暴露到公网。

## 常见问题

- 如果安装后终端无法识别 `uv`，请关闭并重新打开终端。
- 如果 8001 端口已被占用，可改用其他端口，例如 `--port 8011`。
- 如果分析过程中服务器被关闭，请重新创建案件并再次运行分析。
- 如果 DeepSeek 不可用或未配置，PDF 会自动使用本地模板生成。

## 免责声明

FrameJudge 仅展示客观技术比对结果，供人工复核参考。它不提供法律意见，也不判断是否构成著作权侵权。
