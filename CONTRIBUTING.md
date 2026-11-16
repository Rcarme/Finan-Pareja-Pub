# Guía de Contribución / Contributing Guide

¡Gracias por tu interés en contribuir a **Tandem** (Finanzas en Pareja)! Este proyecto es de código abierto bajo la licencia MIT y busca ayudar a parejas y hogares de todo el mundo a gestionar su economía de forma transparente, equitativa y segura.

---

## 🇬🇧 English

### 1. Core Architectural Invariants
When submitting pull requests, please respect the project's core invariants:
- **Money must always be `Decimal`:** Floating point arithmetic is strictly forbidden for currency values. Use `ROUND_HALF_UP` for daily operations and `ROUND_FLOOR` for settlement distributions.
- **Immutable Audit Trail (Append-Only):** Accounting records are never deleted (`DELETE`). Voids and reversals require user attribution and explicit reasons in `audit_log`.
- **Privacy-first & Zero Telemetry:** Self-hosted by design. No tracking or external data transmission (except optional Google Gemini AI API when explicitly configured).
- **Optimistic Concurrency Control:** Account, category, and transaction updates must respect the `version` column.

### 2. Local Development Workflow
1. Fork the repo and clone locally:
   ```bash
   git clone https://github.com/YOUR_USER/Finan-Pareja-Pub.git
   cd Finan-Pareja-Pub
   ```
2. Create and activate a Python 3.11+ virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   .venv\Scripts\activate     # Windows
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.lock
   pip install pytest pytest-cov ruff mypy
   ```
4. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```

### 3. Code Quality & Testing
Before opening a Pull Request, ensure all linters and tests pass:
```bash
# Check code style and linting
ruff check .

# Check automatic formatting
ruff format --check .

# Run test suite
python -m pytest tests/ -v
```

---

## 🇦🇷 Español

### 1. Principios de Diseño No Negociables
Al proponer cambios o nuevas funcionalidades, respetá los pilares fundamentales del sistema:
- **Dinero siempre en `Decimal`:** Prohibido el uso de `float` para importes contables. Usar redondeo estándar `ROUND_HALF_UP` en transacciones y `ROUND_FLOOR` en repartos de cierre.
- **Auditoría inmutable (Append-Only):** Los movimientos nunca se borran físicamente (`DELETE`). Las correcciones se registran con motivo y usuario en `audit_log`.
- **Privacidad y cero telemetría:** El proyecto es 100% autohospedado (self-hosted). Ninguna métrica ni dato sensible se envía a servidores externos (salvo la interacción opcional con la API de Google Gemini solicitada explícitamente por el usuario).
- **Control de concurrencia optimista:** Cualquier modificación sobre cuentas, categorías o transacciones debe respetar la columna `version`.

### 2. Flujo de Trabajo para el Desarrollo Local
1. Hacé un Fork del repositorio y clonalo localmente:
   ```bash
   git clone https://github.com/TU_USUARIO/Finan-Pareja-Pub.git
   cd Finan-Pareja-Pub
   ```
2. Creá un entorno virtual con Python 3.11+:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # En Linux/macOS
   .venv\Scripts\activate     # En Windows
   ```
3. Instalá las dependencias del proyecto:
   ```bash
   pip install -r requirements.lock
   pip install pytest pytest-cov ruff mypy
   ```
4. Creá una rama para tu cambio:
   ```bash
   git checkout -b feature/nombre-de-tu-mejora
   ```

### 3. Calidad de Código y Pruebas
Antes de abrir un Pull Request, asegurate de que el linter y los tests pasen sin errores:
```bash
# Verificar estilo y reglas de calidad
ruff check .

# Verificar formato automático
ruff format --check .

# Ejecutar la suite completa de pruebas unitarias
python -m pytest tests/ -v
```
