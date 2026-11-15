"""Manifiesto íntegro del mismo snapshot PostgreSQL que el dump cifrado."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql


def connect(database: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        dbname=database,
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "finanzas_app"),
        password=os.environ.get("PGPASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
    )


def fingerprint(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    """Incluye todas las filas: detecta diferencias de signos, cuentas y vínculos."""
    result: dict[str, Any] = {}
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    for (name,) in tables:
        statement = sql.SQL(
            "SELECT COUNT(*), md5(COALESCE(string_agg(digest, '' ORDER BY digest), '')) "
            "FROM (SELECT md5(row_to_json(t)::text) AS digest FROM {} t) rows"
        ).format(sql.Identifier("public", name))
        row = connection.execute(statement).fetchone()
        assert row is not None
        result[name] = {"count": row[0], "digest": row[1]}
    return result


def create_snapshot(dump: Path, manifest: Path) -> None:
    database = os.environ.get("POSTGRES_DB", "finanzas")
    with connect(database) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = connection.execute("SELECT pg_export_snapshot()").fetchone()
        assert row is not None
        env = dict(os.environ)
        env.update(
            PGHOST=os.environ.get("POSTGRES_HOST", "localhost"),
            PGPORT=os.environ.get("POSTGRES_PORT", "5432"),
            PGUSER=os.environ.get("POSTGRES_USER", "finanzas_app"),
            PGPASSWORD=os.environ.get("PGPASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
        )
        subprocess.run(
            ["pg_dump", "-Fc", "--snapshot", row[0], "-d", database, "-f", str(dump)],
            env=env,
            check=True,
        )
        manifest.write_text(json.dumps(fingerprint(connection), sort_keys=True), encoding="utf-8")


def verify_snapshot(database: str, manifest: Path) -> None:
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    with connect(database) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if fingerprint(connection) != expected:
            raise ValueError("La restauración no coincide con el manifiesto del snapshot.")


if __name__ == "__main__":
    if sys.argv[1] == "create":
        create_snapshot(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "verify":
        verify_snapshot(sys.argv[2], Path(sys.argv[3]))
    else:
        raise SystemExit("Operación desconocida")
