from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import connections


def _safe_local_db_path(candidate: Path) -> Path:
    filename = candidate.name if candidate.suffix == ".sqlite3" else "default.sqlite3"
    return (Path(settings.BASE_DIR) / "data" / "portfolios" / filename).resolve()


def activate_default_database(db_path: str | Path) -> None:
    target_path = Path(db_path).expanduser()
    try:
        target_path = target_path.resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 다른 머신에서 넘어온 절대경로 등, 현재 환경에서 생성 불가한 DB 경로는
        # 프로젝트 로컬 data/portfolios 디렉터리로 안전하게 우회한다.
        target_path = _safe_local_db_path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
    target_name = str(target_path)
    settings.DATABASES["default"]["NAME"] = target_name
    connections.databases["default"]["NAME"] = target_name
    connections.close_all()
    conn = connections["default"]
    conn.settings_dict["NAME"] = target_name


@contextmanager
def using_default_database(db_path: str | Path):
    conn = connections["default"]
    previous_name = conn.settings_dict.get("NAME")
    activate_default_database(db_path)
    try:
        yield
    finally:
        if previous_name:
            activate_default_database(previous_name)

