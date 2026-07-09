from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

REGISTRY_ENV_KEY = "STOCKAPP_PORTFOLIO_REGISTRY"
PORTFOLIO_DATA_DIR = "portfolios"
DEFAULT_PORTFOLIO_ID = "default"
DEFAULT_PORTFOLIO_NAME = "기본 포트폴리오"


def _default_db_relpath() -> str:
    return "db.sqlite3"


def _to_rel_db_path(db_path: str | Path) -> str:
    raw = str(db_path).strip()
    if not raw:
        return _default_db_relpath()
    candidate = Path(raw).expanduser()
    base_dir = Path(settings.BASE_DIR).resolve()
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
            return str(resolved.relative_to(base_dir)).replace("\\", "/")
        except Exception:
            if candidate.name == "db.sqlite3":
                return _default_db_relpath()
            return f"data/{PORTFOLIO_DATA_DIR}/{candidate.name}"
    return raw.replace("\\", "/")


def _resolve_db_path(db_path: str | Path) -> Path:
    raw = str(db_path).strip()
    if not raw:
        return (Path(settings.BASE_DIR) / _default_db_relpath()).resolve()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path(settings.BASE_DIR) / candidate).resolve()


def _registry_path() -> Path:
    configured = os.getenv(REGISTRY_ENV_KEY)
    if configured:
        return Path(configured).expanduser().resolve()
    return (settings.BASE_DIR / "data" / "portfolios.json").resolve()


def _default_db_path() -> Path:
    configured = os.getenv("STOCKAPP_DB_PATH")
    if configured:
        return _resolve_db_path(configured)
    return (settings.BASE_DIR / "db.sqlite3").resolve()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def ensure_registry_exists() -> None:
    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if registry_path.exists():
        return
    payload = {
        "version": 1,
        "portfolios": [
            {
                "id": DEFAULT_PORTFOLIO_ID,
                "name": DEFAULT_PORTFOLIO_NAME,
                "db_path": _default_db_relpath(),
                "created_at": _now_iso(),
            }
        ],
    }
    registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_registry() -> dict[str, object]:
    ensure_registry_exists()
    registry_path = _registry_path()
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("포트폴리오 레지스트리 형식이 올바르지 않습니다.")
    portfolios = payload.get("portfolios")
    if not isinstance(portfolios, list) or not portfolios:
        payload["portfolios"] = [
            {
                "id": DEFAULT_PORTFOLIO_ID,
                "name": DEFAULT_PORTFOLIO_NAME,
                "db_path": _default_db_relpath(),
                "created_at": _now_iso(),
            }
        ]
        _write_registry(payload)
    changed = False
    for row in payload.get("portfolios", []):
        if not isinstance(row, dict):
            continue
        normalized = _to_rel_db_path(str(row.get("db_path", "")))
        if str(row.get("db_path", "")) != normalized:
            row["db_path"] = normalized
            changed = True
    if changed:
        _write_registry(payload)
    return payload


def _write_registry(payload: dict[str, object]) -> None:
    registry_path = _registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def list_portfolios() -> list[dict[str, str]]:
    payload = _read_registry()
    rows: list[dict[str, str]] = []
    for row in payload.get("portfolios", []):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "id": str(row.get("id", "")),
                "name": str(row.get("name", "")),
                "db_path": str(_resolve_db_path(str(row.get("db_path", "")))),
                "created_at": str(row.get("created_at", "")),
            }
        )
    return [row for row in rows if row["id"] and row["db_path"]]


def get_portfolio(portfolio_id: str) -> dict[str, str] | None:
    target = portfolio_id.strip()
    if not target:
        return None
    for row in list_portfolios():
        if row["id"] == target:
            return row
    return None


def _slugify_name(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not base:
        base = "portfolio"
    return base[:48]


def _next_portfolio_id(name: str, existing_ids: set[str]) -> str:
    base = _slugify_name(name)
    candidate = base
    idx = 2
    while candidate in existing_ids:
        candidate = f"{base}-{idx}"
        idx += 1
    if candidate in existing_ids:
        candidate = f"{base}-{uuid.uuid4().hex[:8]}"
    return candidate


def create_portfolio(name: str) -> dict[str, str]:
    payload = _read_registry()
    portfolios = list_portfolios()
    existing_ids = {row["id"] for row in portfolios}
    portfolio_id = _next_portfolio_id(name, existing_ids)
    db_dir = (settings.BASE_DIR / "data" / PORTFOLIO_DATA_DIR).resolve()
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = (db_dir / f"{portfolio_id}.sqlite3").resolve()
    item = {
        "id": portfolio_id,
        "name": name.strip() or f"포트폴리오-{portfolio_id}",
        "db_path": _to_rel_db_path(db_path),
        "created_at": _now_iso(),
    }
    payload_rows = payload.get("portfolios", [])
    if not isinstance(payload_rows, list):
        payload_rows = []
    payload_rows.append(item)
    payload["portfolios"] = payload_rows
    _write_registry(payload)
    return item


def rename_portfolio(portfolio_id: str, new_name: str) -> dict[str, str] | None:
    payload = _read_registry()
    rows = payload.get("portfolios", [])
    if not isinstance(rows, list):
        return None
    updated: dict[str, str] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("id", "")) != portfolio_id:
            continue
        row["name"] = new_name.strip() or str(row.get("name", ""))
        updated = {
            "id": str(row.get("id", "")),
            "name": str(row.get("name", "")),
            "db_path": str(_resolve_db_path(str(row.get("db_path", "")))),
            "created_at": str(row.get("created_at", "")),
        }
        break
    if updated:
        _write_registry(payload)
    return updated


def delete_portfolio(portfolio_id: str) -> tuple[bool, str]:
    payload = _read_registry()
    rows = payload.get("portfolios", [])
    if not isinstance(rows, list):
        return False, ""
    if len(rows) <= 1:
        return False, "포트폴리오는 최소 1개 이상 유지해야 합니다."

    kept: list[dict[str, object]] = []
    deleted: dict[str, object] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("id", "")) == portfolio_id and deleted is None:
            deleted = row
            continue
        kept.append(row)

    if deleted is None:
        return False, "삭제할 포트폴리오를 찾지 못했습니다."

    payload["portfolios"] = kept
    _write_registry(payload)

    db_path = _resolve_db_path(str(deleted.get("db_path", "")))
    if db_path.exists():
        try:
            db_path.unlink()
        except OSError:
            # DB 파일 삭제 실패는 치명적이지 않으므로 레지스트리 삭제만 유지한다.
            pass
    return True, ""

