#!/usr/bin/env python3
"""Runner multiplataforma para desarrollo, pruebas y utilidades del proyecto."""

import os
import subprocess
import sys
from pathlib import Path

bin_dir = "Scripts" if sys.platform == "win32" else "bin"
VENV_PYTHON = str(Path(f".venv/{bin_dir}/python"))
VENV_PYTEST = str(Path(f".venv/{bin_dir}/pytest"))
VENV_RUFF = str(Path(f".venv/{bin_dir}/ruff"))
VENV_MYPY = str(Path(f".venv/{bin_dir}/mypy"))
VENV_UVICORN = str(Path(f".venv/{bin_dir}/uvicorn"))
VENV_ALEMBIC = str(Path(f".venv/{bin_dir}/alembic"))


def test_env() -> dict[str, str]:
    env = dict(os.environ)
    env["ENVIRONMENT"] = "test"
    return env


def run_cmd(cmd: list[str], *, test_mode: bool = False) -> int:
    print(f"\n>> {' '.join(cmd)}")
    result = subprocess.run(cmd, env=test_env() if test_mode else None)
    return result.returncode


def cmd_test() -> int:
    return run_cmd(
        [
            VENV_PYTEST,
            "-v",
            "--cov=app",
            "--cov-report=term-missing",
        ],
        test_mode=True,
    )


def cmd_check() -> int:
    print("--- 1/3 Ruff Linter ---")
    rc = run_cmd([VENV_RUFF, "check", "."])
    if rc != 0:
        return rc

    print("\n--- 2/3 Mypy Type Checking ---")
    rc = run_cmd([VENV_MYPY, "app"])
    if rc != 0:
        return rc

    print("\n--- 3/3 Tests ---")
    return cmd_test()


def cmd_dev() -> int:
    args = ["app.web.app:app", "--reload", "--host", "127.0.0.1", "--port", "8000"]
    return run_cmd([VENV_UVICORN, *args])


def cmd_migrate() -> int:
    return run_cmd([VENV_ALEMBIC, "upgrade", "head"])


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python run.py [check|test|dev|migrate]")
        return 1

    action = sys.argv[1].lower()
    if action == "check":
        return cmd_check()
    if action == "test":
        return cmd_test()
    if action == "dev":
        return cmd_dev()
    if action == "migrate":
        return cmd_migrate()

    print(f"Comando desconocido: {action}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
