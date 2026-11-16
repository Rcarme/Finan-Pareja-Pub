"""Registra resultados sin interpolar rutas dentro de código Python."""

import os
from datetime import UTC, datetime

from app.core.db import get_session_factory
from app.services.backup_service import record_backup_verification

if __name__ == "__main__":
    with get_session_factory()() as db:
        record_backup_verification(
            db,
            verified_at=datetime.now(UTC),
            status=os.environ["BACKUP_VERIFICATION_STATUS"],
            details={"backup": os.environ.get("VERIFIED_BACKUP", "")},
        )
