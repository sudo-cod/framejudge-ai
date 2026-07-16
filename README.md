# FrameJudge AI / 帧证

FrameJudge is a local AI-assisted video comparison and forensic reporting tool.
It compares one original video with one or more suspected copies, evaluates
visual and audio overlap, and produces an evidence-focused Chinese PDF report.

The application includes a FastAPI backend and a responsive browser interface.
No separate frontend build step is required.

## Features

- One original video compared with multiple suspected videos in one case
- Local MobileNetV2/ONNX fingerprint candidate retrieval
- Strict frame, geometric, timeline, and audio verification
- Picture-in-picture, subtitle, watermark, and occlusion evidence
- Red, yellow, and gray risk classification
- Configurable threshold presets with per-case configuration snapshots
- Evidence screenshots, match timelines, audit details, and technical metrics
- One consolidated PDF report per case
- Local case history with rename, reopen, download, and delete actions

## Privacy and data storage

Video analysis runs locally on the computer hosting FrameJudge. Video frames are
not sent to a cloud AI service.

FrameJudge stores uploaded videos, temporary frames, extracted audio, evidence,
results, and reports under the generated `jobs/` directory. Delete a case from
the interface when its files are no longer needed.

DeepSeek report writing is optional. When enabled, FrameJudge sends a compact
text summary of the analysis results to DeepSeek; it does not send video frames.
Without a DeepSeek API key, the application produces the PDF using its local
report template.

## Requirements

- Windows 10/11 or a recent Linux distribution
- Python 3.11 (the project currently requires `>=3.11,<3.12`)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- Sufficient free disk space for the uploaded videos and temporary analysis files

The required MobileNetV2 ONNX model is included in the repository. FFmpeg is
provided through the `imageio-ffmpeg` dependency.

## Install and run

### Windows PowerShell

```powershell
git clone https://github.com/sudo-cod/framejudge-ai.git
cd framejudge-ai

uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### macOS or Linux

```bash
git clone https://github.com/sudo-cod/framejudge-ai.git
cd framejudge-ai

uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) in a browser.

The first `uv sync` can take several minutes because it installs the video,
audio, OCR, and ONNX dependencies. Later starts only require the final
`uv run uvicorn ...` command.

Press `Ctrl+C` in the terminal to stop the application.

## How to use

1. Open **阈值设置** if you want to choose a strict, standard, loose, or custom
   threshold profile. New settings only apply to cases created afterward.
2. Return to **案件库** and select **新建案件**.
3. Optionally enter a case name.
4. Select exactly one original video.
5. Select one or more suspected videos.
6. Select **开始分析** and wait for all seven analysis stages to finish.
7. Open the case to review the risk overview and select an individual video for
   evidence, timeline, audit, and technical details.
8. Use **下载 PDF 报告** to save the consolidated report.

Analysis time depends heavily on video duration, resolution, and CPU speed.
Large cases can take a long time. Do not stop the server while a case is being
processed.

## Optional DeepSeek reports

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

Then edit `.env`:

```env
DEEPSEEK_API_KEY=your-real-api-key
DEEPSEEK_MODEL=deepseek-chat
```

Restart FrameJudge after changing `.env`. Never commit or share the `.env` file.

## Project layout

```text
app/
  main.py          FastAPI routes, uploads, cases, and job progress
  pipeline.py      Video analysis orchestration
  scoring.py       Thresholds and risk classification
  pdf_report.py    Consolidated PDF generation
  static/          Browser interface
  models/          Bundled local MobileNetV2 ONNX model
pyproject.toml     Python dependencies
uv.lock            Reproducible dependency lockfile
```

The `jobs/`, `threshold-settings.json`, `.env`, caches, and generated reports are
local runtime data and are intentionally excluded from GitHub.

## Local network access

For trusted devices on the same private network, start with:

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Then open `http://<computer-ip>:8001` from another device. FrameJudge currently
has no user authentication, so do not expose this port directly to the public
internet.

## Troubleshooting

- If `uv` is not recognized immediately after installation, close and reopen the
  terminal.
- If port 8001 is busy, choose another port, for example `--port 8011`.
- If a running analysis is interrupted by a server restart, create a new case and
  run the analysis again.
- If DeepSeek is unavailable or not configured, PDF generation automatically
  falls back to the local template.

## Disclaimer

FrameJudge presents objective technical comparison results for review. It does
not provide legal advice and does not determine whether copyright infringement
has occurred.
