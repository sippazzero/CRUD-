"""Tests for container start script behavior and Railway migration contract."""

import json
from pathlib import Path


def test_start_script_structure_and_contracts() -> None:
    """The startup script must run alembic migrations before starting uvicorn."""
    start_sh = Path("scripts/start.sh")
    assert start_sh.is_file(), "scripts/start.sh must exist"

    content = start_sh.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    assert lines[0] == "#!/bin/sh"
    assert "set -e" in lines, "Script must use set -e to exit on migration failure"

    alembic_idx = next(
        i for i, line in enumerate(lines) if "alembic upgrade head" in line
    )
    uvicorn_idx = next(i for i, line in enumerate(lines) if "uvicorn" in line)

    assert alembic_idx < uvicorn_idx, "Alembic migrations must run before Uvicorn"
    uvicorn_line = lines[uvicorn_idx]
    assert "--host 0.0.0.0" in uvicorn_line
    assert "${PORT:-8000}" in uvicorn_line
    assert uvicorn_line.startswith("exec ")


def test_railway_json_start_command() -> None:
    """Railway service configuration must use the start script."""
    railway_json = Path("railway.json")
    assert railway_json.is_file()

    data = json.loads(railway_json.read_text(encoding="utf-8"))
    assert data.get("deploy", {}).get("startCommand") == "/app/scripts/start.sh"


def test_dockerfile_uses_start_script() -> None:
    """Dockerfile must set execute permissions and CMD to scripts/start.sh."""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "chmod +x scripts/start.sh" in dockerfile
    assert 'CMD ["/app/scripts/start.sh"]' in dockerfile
