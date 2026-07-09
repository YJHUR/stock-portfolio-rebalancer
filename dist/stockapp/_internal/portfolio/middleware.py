from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from django.core.management import call_command
from django.db import connections

from .db_context import activate_default_database
from .portfolio_registry import ensure_registry_exists, list_portfolios

SESSION_KEY = "active_portfolio_id"


def _ensure_active_db_schema() -> None:
    conn = connections["default"]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio_holdingsnapshot'"
        )
        exists = cursor.fetchone() is not None
    if exists:
        return
    call_command("migrate", interactive=False, verbosity=0)


class ActivePortfolioMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 단일 DB 환경변수를 직접 지정한 경우 기존 동작을 유지한다.
        explicit_db_path = os.getenv("STOCKAPP_DB_PATH")
        if explicit_db_path:
            resolved = str(Path(explicit_db_path).expanduser().resolve())
            activate_default_database(resolved)
            _ensure_active_db_schema()
            request.portfolio_list = []
            request.active_portfolio = {
                "id": "single",
                "name": "단일 포트폴리오",
                "db_path": resolved,
            }
            return self.get_response(request)

        ensure_registry_exists()
        portfolios = list_portfolios()
        if not portfolios:
            response = self.get_response(request)
            return response

        raw_selected_id = request.COOKIES.get(SESSION_KEY)
        selected_id = unquote(raw_selected_id) if raw_selected_id else None
        active = next((row for row in portfolios if row["id"] == selected_id), portfolios[0])

        activate_default_database(active["db_path"])
        _ensure_active_db_schema()
        request.portfolio_list = portfolios
        request.active_portfolio = active
        return self.get_response(request)

