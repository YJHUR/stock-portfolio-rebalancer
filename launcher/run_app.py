from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from wsgiref.simple_server import make_server


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def _data_root() -> Path:
    configured = os.getenv("STOCKAPP_DATA_DIR")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = (Path.home() / ".stockapp").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bootstrap_default_data(root: Path, data_root: Path) -> None:
    sample_db = (root / "db.sqlite3").resolve()
    target_db = (data_root / "db.sqlite3").resolve()
    registry_path = (data_root / "portfolios.json").resolve()

    if sample_db.exists() and not target_db.exists():
        target_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample_db, target_db)

    default_row = {
        "id": "default",
        "name": "기본 포트폴리오",
        "db_path": str(target_db),
    }

    if not registry_path.exists():
        payload = {"version": 1, "portfolios": [default_row]}
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {"version": 1, "portfolios": [default_row]}
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    portfolios = payload.get("portfolios")
    if not isinstance(portfolios, list) or not portfolios:
        payload["portfolios"] = [default_row]
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    first = portfolios[0] if isinstance(portfolios[0], dict) else None
    if first is None:
        portfolios[0] = default_row
        payload["portfolios"] = portfolios
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    current_db_path = str(first.get("db_path", "")).strip()
    if not current_db_path:
        first["db_path"] = str(target_db)
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    resolved_current = Path(current_db_path).expanduser()
    if not resolved_current.is_absolute():
        resolved_current = (root / resolved_current).resolve()

    if not resolved_current.exists() and target_db.exists():
        first["db_path"] = str(target_db)
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_http_url(url: str) -> str:
    if "://" in url:
        return url
    return f"http://{url}"


def _open_browser(url: str) -> None:
    safe_url = _ensure_http_url(url)
    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["open", safe_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    webbrowser.open(safe_url)


def _should_use_https() -> bool:
    default = "1" if sys.platform == "darwin" else "0"
    return os.getenv("STOCKAPP_USE_HTTPS", default).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_https_cert(data_root: Path) -> tuple[Path, Path]:
    bundled_cert_path = (Path(__file__).resolve().parent / "certs" / "localhost-cert.pem").resolve()
    bundled_key_path = (Path(__file__).resolve().parent / "certs" / "localhost-key.pem").resolve()
    if bundled_cert_path.exists() and bundled_key_path.exists():
        return bundled_cert_path, bundled_key_path

    cert_dir = (data_root / "certs").resolve()
    try:
        cert_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cert_dir = (Path(tempfile.gettempdir()) / "stockapp-certs").resolve()
        cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = (cert_dir / "localhost-cert.pem").resolve()
    key_path = (cert_dir / "localhost-key.pem").resolve()
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    # SAN을 포함한 로컬 self-signed 인증서를 생성한다.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "3650",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert_path, key_path


def _serve_https(port: int, data_root: Path) -> None:
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.wsgi import get_wsgi_application

    cert_path, key_path = _ensure_https_cert(data_root)
    application = StaticFilesHandler(get_wsgi_application())
    server = make_server("0.0.0.0", port, application)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main() -> None:
    root = _project_root()
    sys.path.insert(0, str(root))

    data_root = _data_root()
    _bootstrap_default_data(root, data_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault(
        "STOCKAPP_PORTFOLIO_REGISTRY",
        str((data_root / "portfolios.json").resolve()),
    )
    os.environ.setdefault("STOCKAPP_DB_PATH", str((data_root / "db.sqlite3").resolve()))

    import django
    from django.core.management import call_command, execute_from_command_line

    django.setup()
    call_command("migrate", interactive=False, verbosity=0)

    port = int(os.getenv("STOCKAPP_PORT", "60600"))
    url = f"http://localhost:{port}/"
    print(f"Open this URL in your browser: {url}", flush=True)
    threading.Timer(1.2, lambda: _open_browser(url)).start()
    execute_from_command_line(["manage.py", "runserver", f"0.0.0.0:{port}", "--noreload"])


if __name__ == "__main__":
    main()

