"""Admin-only settings endpoints.

Backed by config/local.toml — GET returns the currently-effective values
for the fields whitelisted below, PATCH validates a partial update,
writes through to local.toml, and invalidates the cached Settings so
subsequent requests see the new values.

What's editable here is intentionally a subset of the full Settings
surface: anything that would require restarting uvicorn / would break
running connections (server.host/port, security.secret_key, paths.*)
stays out of the UI and is configured via local.toml by hand.
"""

from __future__ import annotations

import copy
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, HTTPException, status

from ..config import get_settings, update_local_settings

router = APIRouter(prefix="/admin/settings", tags=["admin", "settings"])


# Per-field metadata:
#   type:    string | int | float | string_list
#   min/max: numeric bounds (optional)
#   choices: allowed string values (optional)
#   restart: None (hot-reload), "worker" (needs worker restart), "api" or "all"
#   help:    short Korean description for the UI
EDITABLE: dict[str, dict[str, dict[str, Any]]] = {
    "app": {
        "name": {
            "type": "string",
            "restart": None,
            "help": "헤더와 로그인 페이지에 표시되는 앱 이름.",
        },
        "display_timezone": {
            "type": "string",
            "restart": None,
            "help": "예: Asia/Seoul. 표시용 (저장값은 항상 UTC).",
        },
    },
    "map": {
        "nearby_radius_deg": {
            "type": "float",
            "min": 0.0001,
            "max": 1.0,
            "restart": None,
            "help": "단일 사진 마커(count=1) 클릭 시 라이트박스 prev/next에 채울 근처 사진 반경. 0.005 ≈ 500m. 클러스터 우클릭/최대줌 클릭은 셀 단위 정확 조회라 이 값과 무관.",
        },
        "nearby_limit": {
            "type": "int",
            "min": 1,
            "max": 500,
            "restart": None,
            "help": "단일 마커 클릭 시 prev/next 리스트 최대 개수. 클러스터에는 적용되지 않음.",
        },
    },
    "worker": {
        "concurrency": {
            "type": "int",
            "min": 1,
            "max": 32,
            "restart": "worker",
            "help": "동시 처리 스레드 수. NAS HDD면 3~4가 더 빠를 수 있음.",
        },
        "idle_poll_seconds": {
            "type": "int",
            "min": 1,
            "max": 60,
            "restart": "worker",
            "help": "큐가 비었을 때 워커가 다음 폴링까지 대기하는 초.",
        },
        "job_lease_seconds": {
            "type": "int",
            "min": 60,
            "max": 3600,
            "restart": "worker",
            "help": "running 잡이 좀비로 간주되어 재할당되기까지의 초.",
        },
    },
    "thumbnails": {
        "quality": {
            "type": "int",
            "min": 50,
            "max": 100,
            "restart": None,
            "help": "JPEG 썸네일 품질 (50–100). 변경 후 새로 생성되는 썸네일에만 적용.",
        },
    },
    "exif": {
        "extractor_chain": {
            "type": "string_list",
            "restart": None,
            "help": "EXIF 추출기 순서 — 쉼표로 구분 (예: pillow, exiftool).",
        },
    },
    "logging": {
        "level": {
            "type": "string",
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
            "restart": "all",
            "help": "로깅 레벨. API/워커 재시작 필요.",
        },
    },
    "scanner": {
        "ignore_dirs": {
            "type": "string_list",
            "restart": None,
            "help": "스캔에서 건너뛸 디렉토리 이름 (쉼표 구분).",
        },
        "ignore_files": {
            "type": "string_list",
            "restart": None,
            "help": "스캔에서 건너뛸 파일 이름 (쉼표 구분).",
        },
        "image_extensions": {
            "type": "string_list",
            "restart": None,
            "help": "인덱싱할 이미지 확장자 (소문자, 점 없이).",
        },
        "video_extensions": {
            "type": "string_list",
            "restart": None,
            "help": "인덱싱할 동영상 확장자 (소문자, 점 없이).",
        },
    },
}


def _coerce(section: str, key: str, value: Any, spec: dict[str, Any]) -> Any:
    """Validate + coerce one incoming value against the field's spec."""
    typ = spec["type"]
    label = f"{section}.{key}"

    if typ == "string":
        if not isinstance(value, str):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label}: 문자열이어야 합니다"
            )
        value = value.strip()
        if "choices" in spec and value not in spec["choices"]:
            choices = spec["choices"]
            shown = choices if len(choices) <= 10 else f"{choices[:10]} 등 {len(choices)}개"
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}: {shown} 중 하나여야 합니다",
            )
        return value

    if typ in ("int", "float"):
        try:
            value = int(value) if typ == "int" else float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label}: 숫자여야 합니다"
            )
        if "min" in spec and value < spec["min"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}: 최소 {spec['min']} 이상이어야 합니다",
            )
        if "max" in spec and value > spec["max"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}: 최대 {spec['max']} 이하여야 합니다",
            )
        return value

    if typ == "string_list":
        if isinstance(value, str):
            # Accept comma- or newline-delimited input from a single text field.
            parts = [p.strip() for p in value.replace("\n", ",").split(",")]
            value = [p for p in parts if p]
        if not isinstance(value, list):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label}: 리스트여야 합니다"
            )
        return [str(x) for x in value]

    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, f"{label}: 알 수 없는 타입 {typ}"
    )


def _schema_with_dynamic_choices() -> dict[str, dict[str, dict[str, Any]]]:
    """Return EDITABLE with dynamic option lists (timezone catalog, etc.)
    spliced in. Deep-copied so we never mutate the module-level catalog."""
    schema = copy.deepcopy(EDITABLE)
    # IANA timezones — sorted for a usable native <select>. Browsers do
    # prefix-match keyboard navigation, so alphabetical is fine.
    schema["app"]["display_timezone"]["choices"] = sorted(available_timezones())
    return schema


@router.get("")
def read_settings() -> dict[str, Any]:
    s = get_settings()
    schema = _schema_with_dynamic_choices()
    current: dict[str, dict[str, Any]] = {}
    for section, fields in schema.items():
        sec_obj = getattr(s, section, None)
        if sec_obj is None:
            continue
        current[section] = {}
        for key in fields:
            if hasattr(sec_obj, key):
                current[section][key] = getattr(sec_obj, key)
    return {"schema": schema, "current": current}


@router.patch("")
def patch_settings(payload: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "본문이 객체여야 합니다")

    schema = _schema_with_dynamic_choices()
    validated: dict[str, dict[str, Any]] = {}
    for section, fields in payload.items():
        if section not in schema:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"섹션 '{section}'은 UI에서 편집할 수 없습니다",
            )
        if not isinstance(fields, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"섹션 '{section}'의 값이 객체여야 합니다"
            )
        sec_spec = schema[section]
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in sec_spec:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"키 '{section}.{k}'은 UI에서 편집할 수 없습니다",
                )
            out[k] = _coerce(section, k, v, sec_spec[k])
        if out:
            validated[section] = out

    if not validated:
        return {"ok": True, "updated_sections": []}

    update_local_settings(validated)
    return {
        "ok": True,
        "updated_sections": sorted(validated.keys()),
    }
