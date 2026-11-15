#!/usr/bin/env bash
# ops/backup/backup_test.sh — Runner de prueba de backup y verificación (Makefile target)
set -euo pipefail

PYTHON="${PYTHON:-python}"
if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -f ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"
fi

ENVIRONMENT=test "${PYTHON}" scripts/backup_test.py
