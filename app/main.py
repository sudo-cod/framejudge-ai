"""FastAPI 入口：上传原版+涉案视频，后台线程分析，轮询进度，返回结果与证据。"""
import json
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .pipeline import run_job
from .pdf_report import PDF_FILENAME, PDF_VERSION, generate_case_pdf
from .report import CASE_SUMMARY_VERSION, generate_case_report
from .settings import (load_settings, preset_label, public_settings,
                       save_settings)

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="帧证 FrameJudge — AI辅助视频比对取证")

# 内存态任务表：job_id -> progress dict（重启后靠 result.json 恢复已完成任务）
_jobs: dict[str, dict] = {}


class CaseNameUpdate(BaseModel):
    name: str


class ThresholdSettingsUpdate(BaseModel):
    preset: str
    values: dict | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _case_dir(case_id: str) -> Path:
    path = (JOBS_DIR / case_id).resolve()
    if path.parent != JOBS_DIR.resolve():
        raise HTTPException(404, "案件不存在")
    return path


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _update_case(case_dir: Path, **updates) -> dict:
    path = case_dir / "case.json"
    metadata = _read_json(path, {}) or {}
    metadata.update(updates)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return metadata


def _legacy_case_metadata(case_dir: Path, payload: dict) -> dict:
    original = next(case_dir.glob("original.*"), None)
    created = datetime.fromtimestamp(
        case_dir.stat().st_ctime).astimezone().isoformat(timespec="seconds")
    return {
        "case_id": case_dir.name,
        "name": f"历史案件 {case_dir.name}",
        "original_filename": original.name if original else "未记录",
        "suspect_count": len(payload.get("results", [])),
        "created_at": created,
        "updated_at": created,
        "status": "completed",
    }


def _ensure_case_result(case_dir: Path) -> dict | None:
    """Load a case and lazily give legacy batches one consolidated report."""
    result_path = case_dir / "result.json"
    payload = _read_json(result_path)
    if not isinstance(payload, dict) or "results" not in payload:
        return None
    metadata = _read_json(case_dir / "case.json")
    if not metadata:
        metadata = _legacy_case_metadata(case_dir, payload)
        _update_case(case_dir, **metadata)
    payload["case"] = metadata
    changed = False
    if ("report" not in payload or
            payload["report"].get("summary_version") != CASE_SUMMARY_VERSION):
        text, source = generate_case_report(
            metadata, payload.get("results", []), payload.get("errors", []))
        payload["report"] = {**payload.get("report", {}),
                             "text": text, "source": source,
                             "summary_version": CASE_SUMMARY_VERSION}
        changed = True
    pdf_path = case_dir / PDF_FILENAME
    if (not pdf_path.is_file() or
            payload["report"].get("pdf_version") != PDF_VERSION):
        generate_case_pdf(
            case_dir, metadata, payload.get("results", []),
            payload.get("errors", []), payload["report"]["text"],
            payload["report"].get("source", "template"))
    if (payload["report"].get("pdf") != PDF_FILENAME or
            payload["report"].get("pdf_version") != PDF_VERSION):
        payload["report"].update(
            {"pdf": PDF_FILENAME, "pdf_version": PDF_VERSION})
        changed = True
    if changed:
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _case_summary(case_dir: Path) -> dict | None:
    metadata = _read_json(case_dir / "case.json")
    result_path = case_dir / "result.json"
    payload = _read_json(result_path)
    if not metadata:
        if not isinstance(payload, dict) or "results" not in payload:
            return None
        metadata = _legacy_case_metadata(case_dir, payload)
    status = metadata.get("status", "processing")
    if result_path.is_file() and isinstance(payload, dict) and "results" in payload:
        status = "completed"
    elif case_dir.name not in _jobs and status == "processing":
        status = "interrupted"
    counts = {"red": 0, "yellow": 0, "gray": 0}
    completed = 0
    failed = 0
    if isinstance(payload, dict) and "results" in payload:
        completed = len(payload.get("results", []))
        failed = len(payload.get("errors", []))
        for item in payload.get("results", []):
            color = item.get("result", {}).get("tier", {}).get("color")
            if color in counts:
                counts[color] += 1
    return {**metadata, "case_id": case_dir.name, "status": status,
            "completed_count": completed, "failed_count": failed,
            "risk_counts": counts}


def _safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "video.mp4").suffix.lower()
    return suffix if suffix and len(suffix) <= 10 else ".mp4"


def _run_batch(batch_dir: Path, original_path: str,
               suspects: list[tuple[str, str, str]], progress: dict,
               thresholds: dict) -> None:
    """Compare every suspect with one original and publish one batch result."""
    results = []
    errors = []
    total = len(suspects)
    try:
        for index, (result_id, display_name, suspect_path) in enumerate(suspects, 1):
            child_dir = batch_dir / result_id
            child_dir.mkdir(parents=True, exist_ok=True)

            class ChildProgress(dict):
                """Mirror a child's current pipeline stage into batch progress."""

                def update(self, *args, **kwargs):
                    super().update(*args, **kwargs)
                    stage = self.get("stage")
                    if stage and stage != "完成":
                        progress.update(current=index, current_name=display_name,
                                        stage=stage)

            child_progress = ChildProgress(
                stage="", status="running", error=None)
            progress.update(current=index, current_name=display_name,
                            stage=f"{index}/{total} · preparing")
            run_job(child_dir, original_path, suspect_path, child_progress,
                    include_report=False, thresholds=thresholds)
            if child_progress["status"] == "done":
                result = json.loads(
                    (child_dir / "result.json").read_text(encoding="utf-8"))
                results.append({"result_id": result_id, "filename": display_name,
                                "result": result})
            else:
                errors.append({"filename": display_name,
                               "error": child_progress["error"] or "Analysis failed"})
        metadata = _update_case(
            batch_dir, status="completed", completed_at=_now(),
            updated_at=_now(), completed_count=len(results),
            failed_count=len(errors))
        report_text, report_source = generate_case_report(
            metadata, results, errors)
        generate_case_pdf(batch_dir, metadata, results, errors,
                          report_text, report_source)
        (batch_dir / "result.json").write_text(
            json.dumps({"case": metadata, "results": results,
                        "errors": errors,
                        "report": {"text": report_text,
                                   "source": report_source,
                                   "summary_version": CASE_SUMMARY_VERSION,
                                   "pdf": PDF_FILENAME,
                                   "pdf_version": PDF_VERSION}},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        progress.update(stage="完成", status="done", current=total)
    except Exception as exc:
        _update_case(batch_dir, status="failed", updated_at=_now(),
                     error=f"{type(exc).__name__}: {exc}")
        progress.update(status="error", error=f"{type(exc).__name__}: {exc}")


@app.post("/api/jobs")
async def create_job(original: UploadFile, suspect: UploadFile):
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    orig_path = job_dir / ("original" + Path(original.filename or "o.mp4").suffix)
    susp_path = job_dir / ("suspect" + Path(suspect.filename or "s.mp4").suffix)
    for upload, dest in ((original, orig_path), (suspect, susp_path)):
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)

    progress = {"stage": "排队中", "status": "running", "error": None}
    _jobs[job_id] = progress
    thresholds = load_settings()["values"]
    threading.Thread(target=run_job,
                     args=(job_dir, str(orig_path), str(susp_path), progress,
                           True, thresholds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/batches")
@app.post("/api/cases")
async def create_case(
    original: Annotated[UploadFile, File()],
    suspects: Annotated[list[UploadFile], File()],
    case_name: Annotated[str, Form()] = "",
):
    if not suspects:
        raise HTTPException(400, "At least one comparison video is required")
    batch_id = uuid.uuid4().hex[:12]
    batch_dir = JOBS_DIR / batch_id
    batch_dir.mkdir(parents=True)
    created_at = _now()
    active_settings = load_settings()
    thresholds = active_settings["values"]
    display_name = case_name.strip() or f"案件 {created_at[:10]} {created_at[11:16]}"
    _update_case(
        batch_dir, case_id=batch_id, name=display_name,
        original_filename=original.filename or "original.mp4",
        suspect_count=len(suspects), created_at=created_at,
        updated_at=created_at, status="processing",
        threshold_preset=active_settings["preset"],
        threshold_preset_label=preset_label(active_settings["preset"]),
        threshold_revision=active_settings["revision"],
        threshold_schema_version=active_settings["schema_version"],
        thresholds=thresholds)
    original_path = batch_dir / ("original" + _safe_suffix(original.filename))
    with original_path.open("wb") as output:
        shutil.copyfileobj(original.file, output)

    saved: list[tuple[str, str, str]] = []
    for index, upload in enumerate(suspects, 1):
        result_id = f"video_{index:03d}"
        path = batch_dir / (result_id + _safe_suffix(upload.filename))
        with path.open("wb") as output:
            shutil.copyfileobj(upload.file, output)
        saved.append((result_id, upload.filename or path.name, str(path)))

    progress = {"stage": "排队中", "status": "running", "error": None,
                "current": 0, "total": len(saved), "current_name": None}
    _jobs[batch_id] = progress
    threading.Thread(target=_run_batch,
                     args=(batch_dir, str(original_path), saved, progress,
                           thresholds),
                     daemon=True).start()
    return {"job_id": batch_id, "case_id": batch_id,
            "name": display_name, "count": len(saved)}


@app.get("/api/cases")
async def list_cases():
    cases = []
    for case_dir in JOBS_DIR.iterdir():
        if not case_dir.is_dir() or case_dir.name.startswith("_"):
            continue
        summary = _case_summary(case_dir)
        if summary:
            cases.append(summary)
    cases.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"cases": cases}


@app.get("/api/settings")
async def get_threshold_settings():
    return public_settings()


@app.put("/api/settings")
async def update_threshold_settings(update: ThresholdSettingsUpdate):
    try:
        settings = save_settings(update.preset, update.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"settings": settings}


@app.get("/api/cases/{case_id}")
async def case_detail(case_id: str):
    case_dir = _case_dir(case_id)
    payload = _ensure_case_result(case_dir)
    if payload is None:
        progress = _jobs.get(case_id)
        if progress:
            return {"case": _read_json(case_dir / "case.json", {}),
                    "status": progress}
        raise HTTPException(404, "案件不存在或尚未生成结果")
    return JSONResponse(payload)


@app.patch("/api/cases/{case_id}")
async def rename_case(case_id: str, update: CaseNameUpdate):
    case_dir = _case_dir(case_id)
    if not case_dir.is_dir():
        raise HTTPException(404, "案件不存在")
    name = update.name.strip()
    if not name:
        raise HTTPException(400, "案件名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "案件名称不能超过 100 个字符")
    metadata = _update_case(case_dir, name=name, updated_at=_now())
    payload = _ensure_case_result(case_dir)
    if payload is not None:
        payload["case"] = metadata
        report = payload["report"]
        generate_case_pdf(
            case_dir, metadata, payload.get("results", []),
            payload.get("errors", []), report["text"],
            report.get("source", "template"))
        (case_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"case": _case_summary(case_dir)}


@app.get("/api/cases/{case_id}/report")
async def case_report(case_id: str):
    payload = _ensure_case_result(_case_dir(case_id))
    if payload is None or "report" not in payload:
        raise HTTPException(404, "案件报告尚未生成")
    path = _case_dir(case_id) / payload["report"].get("pdf", PDF_FILENAME)
    if not path.is_file():
        raise HTTPException(404, "案件 PDF 报告尚未生成")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"case-report-{case_id}.pdf")


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str):
    case_dir = _case_dir(case_id)
    progress = _jobs.get(case_id)
    if progress and progress.get("status") == "running":
        raise HTTPException(409, "案件正在分析中，完成后才能删除")
    if not case_dir.is_dir():
        raise HTTPException(404, "案件不存在")
    # _case_dir guarantees this is one direct child of JOBS_DIR.
    shutil.rmtree(case_dir)
    _jobs.pop(case_id, None)
    return {"deleted": True, "case_id": case_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    progress = _jobs.get(job_id)
    result_file = JOBS_DIR / job_id / "result.json"
    if progress is None and not result_file.exists():
        raise HTTPException(404, "任务不存在")
    if result_file.exists():
        return {"status": "done", "stage": "完成"}
    return progress


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str):
    result_file = JOBS_DIR / job_id / "result.json"
    if not result_file.exists():
        raise HTTPException(404, "结果尚未生成")
    payload = _ensure_case_result(JOBS_DIR / job_id)
    if payload is not None:
        return JSONResponse(payload)
    return JSONResponse(json.loads(result_file.read_text(encoding="utf-8")))


@app.get("/api/jobs/{job_id}/evidence/{name}")
async def evidence_image(job_id: str, name: str):
    path = (JOBS_DIR / job_id / "evidence" / name).resolve()
    if not path.is_file() or JOBS_DIR not in path.parents:
        raise HTTPException(404, "证据文件不存在")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/results/{result_id}/evidence/{name}")
async def batch_evidence_image(job_id: str, result_id: str, name: str):
    path = (JOBS_DIR / job_id / result_id / "evidence" / name).resolve()
    if not path.is_file() or JOBS_DIR.resolve() not in path.parents:
        raise HTTPException(404, "Evidence file does not exist")
    return FileResponse(path)


app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True),
          name="static")
