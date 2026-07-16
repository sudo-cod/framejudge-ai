"""Persistent threshold presets and validation for new cases."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "threshold-settings.json"
SETTINGS_SCHEMA_VERSION = 1

STANDARD_THRESHOLDS = {
    "fuse_visual_ratio": 0.80,
    "fuse_audio_ratio": 0.70,
    "fuse_continuous_seconds": 30,
    "fuse_density_ratio": 0.30,
    "penalty_yellow_min": 40,
    "subtitle_min_hits": 3,
    "watermark_min_hits": 4,
}

PRESETS = {
    "strict": {
        "label": "严格",
        "description": "更容易标记风险，适合初筛。",
        "values": {**STANDARD_THRESHOLDS,
                   "fuse_visual_ratio": 0.70,
                   "fuse_audio_ratio": 0.60,
                   "fuse_continuous_seconds": 20,
                   "fuse_density_ratio": 0.20,
                   "penalty_yellow_min": 30,
                   "subtitle_min_hits": 2,
                   "watermark_min_hits": 3},
    },
    "standard": {
        "label": "标准",
        "description": "系统默认判定口径。",
        "values": deepcopy(STANDARD_THRESHOLDS),
    },
    "lenient": {
        "label": "宽松",
        "description": "需要更强重合证据才标记风险。",
        "values": {**STANDARD_THRESHOLDS,
                   "fuse_visual_ratio": 0.90,
                   "fuse_audio_ratio": 0.85,
                   "fuse_continuous_seconds": 45,
                   "fuse_density_ratio": 0.45,
                   "penalty_yellow_min": 50,
                   "subtitle_min_hits": 5,
                   "watermark_min_hits": 6},
    },
}

LIMITS = {
    "fuse_visual_ratio": (0.05, 1.0),
    "fuse_audio_ratio": (0.05, 1.0),
    "fuse_continuous_seconds": (1, 600),
    "fuse_density_ratio": (0.01, 1.0),
    "penalty_yellow_min": (1, 110),
    "subtitle_min_hits": (1, 24),
    "watermark_min_hits": (1, 24),
}

_INTEGER_FIELDS = {
    "fuse_continuous_seconds", "penalty_yellow_min",
    "subtitle_min_hits", "watermark_min_hits",
}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_thresholds(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("阈值配置格式不正确")
    cleaned = {}
    for key, default in STANDARD_THRESHOLDS.items():
        raw = values.get(key, default)
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是数字") from exc
        minimum, maximum = LIMITS[key]
        if not minimum <= number <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        cleaned[key] = int(round(number)) if key in _INTEGER_FIELDS else round(number, 4)
    return cleaned


def _default_payload() -> dict:
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "revision": 1,
        "preset": "standard",
        "values": deepcopy(STANDARD_THRESHOLDS),
        "updated_at": None,
    }


def load_settings() -> dict:
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        payload["values"] = validate_thresholds(payload.get("values", {}))
        payload.setdefault("revision", 1)
        payload.setdefault("preset", "custom")
        payload.setdefault("schema_version", SETTINGS_SCHEMA_VERSION)
        return payload
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return _default_payload()


def save_settings(preset: str, values: dict | None = None) -> dict:
    if preset in PRESETS:
        cleaned = deepcopy(PRESETS[preset]["values"])
    elif preset == "custom":
        cleaned = validate_thresholds(values or {})
    else:
        raise ValueError("未知的阈值预设")
    with _lock:
        current = load_settings()
        payload = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "revision": int(current.get("revision", 0)) + 1,
            "preset": preset,
            "values": cleaned,
            "updated_at": _now(),
        }
        SETTINGS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def public_settings() -> dict:
    return {
        "settings": load_settings(),
        "presets": PRESETS,
        "limits": {key: {"min": value[0], "max": value[1]}
                   for key, value in LIMITS.items()},
    }


def preset_label(preset: str) -> str:
    return PRESETS.get(preset, {}).get("label", "自定义")
