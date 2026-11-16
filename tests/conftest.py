import os
import sys
from pathlib import Path

# Asegurar que el root del proyecto esté en sys.path al invocar pytest directamente
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Forzar entorno de test y clave antes de cargar la app o settings
os.environ["ENVIRONMENT"] = "test"
os.environ["APP_SECRET_KEY"] = "test-secret-key-with-at-least-32-chars-long!"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")
