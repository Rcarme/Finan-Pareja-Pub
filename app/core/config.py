"""Configuración de la aplicación cargada desde variables de entorno."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración central de la app finanzas."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENVIRONMENT: str = "production"
    APP_SECRET_KEY: str | None = None
    DATABASE_URL: str = ""
    TEST_DATABASE_URL: str = ""
    MIGRATION_DATABASE_URL: str | None = None

    REMOTE_DATABASE_HOST: str | None = None
    LOCAL_DATABASE_HOST: str | None = None
    DATABASE_PORT: int = 5432

    APP_TIMEZONE: str = "America/Argentina/Buenos_Aires"
    DEFAULT_CURRENCY: str = "ARS"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    APP_HOST_PORT: int = 8088
    APP_BIND_ADDRESS: str = "127.0.0.1"
    FX_AUTOMATIC_ENABLED: bool = True

    # Google Gemini AI - Opcional
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Age y Rclone - Opcional
    AGE_PUBLIC_KEY: str | None = None
    RCLONE_REMOTE: str = "gdrive:finanzas_backups"
    BACKUP_TASK_TIMEOUT_SECONDS: int = 7200
    BACKUP_VERIFY_RETRY_SECONDS: int = 3600

    @model_validator(mode="after")
    def validate_database_urls(self) -> "Settings":
        """Valida que la configuración mínima esté presente."""
        if not self.DATABASE_URL or not self.DATABASE_URL.strip():
            # Permitir arranque si se corre alembic o tests con URL específica
            pass
        secret = (self.APP_SECRET_KEY or "").strip()
        lowered = secret.lower()
        if self.ENVIRONMENT not in ("test", "development") and (
            not secret or len(secret) < 16 or "replace" in lowered or "change-me" in lowered
        ):
            raise ValueError(
                "APP_SECRET_KEY no está configurada o es un placeholder inseguro. "
                "Definila en el archivo .env con un valor aleatorio de 32+ caracteres."
            )
        return self


settings = Settings()
