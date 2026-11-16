# Arquitectura del Sistema (ARCHITECTURE.md)

Este documento describe formalmente la arquitectura de software, el modelo de datos, la seguridad y los principios de contabilidad del sistema **Tandem** (Finanzas en Pareja).

---

## 1. Visión General y Filosofía de Diseño

El sistema está diseñado específicamente para hogares y parejas con los siguientes pilares no negociables:

1. **Exactitud Contable Inquebrantable:** Prohibido el uso de tipos de coma flotante (`float`). Todo el dinero se modela y calcula mediante `Decimal` con redondeo bancario explícito.
2. **Auditoría Append-Only:** Nada se elimina contablemente. Los errores se compensan con reversiones o anulaciones que preservan el historial inmutable con usuario, fecha y motivo en `audit_log`.
3. **Privacidad y Auto-alojamiento (Self-Hosted):** Despliegue en homelabs o servidores privados sin telemetría de terceros ni fuga de datos personales.
4. **UI Dual Adaptativa:**
   - **Modo Simple (Mobile-First a 390px):** Carga rápida de gastos en la vía pública, reconocimiento de voz integrado y verificación de saldos.
   - **Modo Completo:** Conciliación de resúmenes de tarjetas, arrastre de saldos, cierres mensuales formales y reportes.
5. **Inteligencia Artificial Pragmática:** Asistente con Google Gemini para desglosar tickets y compras en lenguaje natural, con detección automática de gastos duplicados antes de confirmar.

---

## 2. Stack Tecnológico

- **Backend:** Python 3.11+ con FastAPI (asíncrono, OpenAPI nativo).
- **Servidor ASGI:** Uvicorn / Gunicorn con worker Uvicorn.
- **ORM y Persistencia:** SQLAlchemy 2.0 (modo declarativo Mapped) + Alembic para migraciones DDL.
- **Base de Datos:** PostgreSQL 16 con separación de roles de privilegios mínimos.
- **Seguridad Criptográfica:** Argon2id (hashing de contraseñas) y `age` (cifrado asimétrico de backups).
- **Frontend:** Jinja2 templates server-rendered con CSS vanilla Mobile-First, Web Speech API para dictado por voz y Service Worker (PWA básica).

---

## 3. Modelo de Dominio y Contabilidad

### 3.1. Tipos de Cuentas y Fondos
- **Cuentas Físicas (`Account`):** Bancos, billeteras virtuales, efectivo y tarjetas de crédito.
- **Fondo Común:** Dinero compartido destinado a gastos del hogar.
- **Dinero Personal (`OwnershipEntry`):** Asignaciones individuales de cada integrante dentro de cuentas compartidas o particulares.
- **Sobres Virtuales (`Envelope`):** Reservas etiquetadas dentro del fondo común (Ahorro común, Colchón de emergencia, Compromisos).

### 3.2. Reglas y Convenciones de Redondeo Contable
- **Transacciones Diarias, Cotizaciones y Métricas Analíticas:** Se utiliza redondeo aritmético/bancario estándar `ROUND_HALF_UP` a 2 decimales para dinero (`Decimal("0.01")`) y a 4 decimales para cotizaciones cambiarias (`Decimal("0.0001")`).
- **Liquidación de Cierres Mensuales y Repartos (`settlement.py`, `close_engine.py`, `spending.py`):** Se aplica deliberadamente redondeo conservador truncado hacia abajo (`ROUND_FLOOR` / `ROUND_DOWN`). Esta es una decisión formal de diseño: en ningún caso se deben repartir centavos fraccionarios inexistentes que desbalanceen el fondo real disponible. Cualquier diferencia residual indivisible permanece en el fondo acumulado del hogar.

### 3.3. Control de Concurrencia (Optimistic Locking)
Las entidades críticas (`Account`, `Category`, `Transaction`) implementan `OptimisticLockMixin` con una columna `version: int`. Cualquier escritura verifica que la versión esperada coincida; de lo contrario, se rechaza la transacción con `ConcurrencyError`.

---

## 4. Seguridad y Autenticación

### 4.1. Hashing de Contraseñas
Se utiliza **Argon2id** (`PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1)`). Las contraseñas exigen un mínimo estricto de 12 caracteres.

### 4.2. Sesiones Server-Side
- Al iniciar sesión, se genera un token criptográfico de 256 bits (`secrets.token_hex(32)`).
- En la base de datos se almacena únicamente el hash SHA-256 del token en la tabla `sessions`.
- Las cookies de sesión son `HttpOnly`, `SameSite=Lax`, con prefijo `Secure` en producción y caducidad deslizante de 30 días.

### 4.3. Protecciones Web
- **CSRF:** Validación de tokens criptográficos por formulario y en cabeceras `X-CSRF-Token` mediante `secrets.compare_digest`.
- **Content Security Policy (CSP):** CSP estricta con `nonce` único por solicitud para `<script>` sin `unsafe-inline`.
- **Cabeceras HTTP:** `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `object-src 'none'`.
- **Rate Limiting:** Control de intentos fallidos en login por IP y usuario (máximo 5 intentos en 15 minutos).
- **Body Limit:** `RequestBodyLimitMiddleware` limita peticiones a 2.1 MB para prevenir ataques DoS por agotamiento de memoria.

### 4.4. Inmutabilidad de Auditoría y Separación de Roles en PostgreSQL
- **Rol de Aplicación (`finanzas_app`):** Solo tiene permisos `SELECT, INSERT` sobre `audit_log`. Se le revocan explícitamente `UPDATE`, `DELETE`, `TRUNCATE` y `TRIGGER`.
- **Rol de Migraciones (`finanzas_owner`):** Solo tiene permisos `SELECT, INSERT` sobre `audit_log`. Tampoco posee privilegios de mutación ni de trigger.
- **Ownership de `audit_log`:** La tabla pertenece al superusuario de aprovisionamiento inicial (`finanzas_bootstrap`). Ni la aplicación web en runtime ni el ejecutor de migraciones DDL pueden alterar su estructura ni desactivar los triggers de inmutabilidad (`trg_audit_log_protect_row` y `trg_audit_log_protect_truncate`).

---

## 5. Asistente de Configuración Inicial (Onboarding)

1. En el arranque inicial, `AuthenticationMiddleware` consulta si existen usuarios (`select count(id) from users`).
2. Si el conteo es cero, cualquier petición HTTP no estática se redirige a `/setup`.
3. **Protección contra tomas de control previa:** En entornos de producción (`ENVIRONMENT=production`), el asistente `/setup` exige obligatoriamente la configuración y validación de `SETUP_TOKEN` en `.env`. Si no está configurado, `/setup` responde HTTP 403 Forbidden impidiendo que un tercero inicialice una instancia expuesta antes de tiempo.
4. El wizard de setup realiza una transacción atómica protegida:
   - Crea Persona 1 y Persona 2 con contraseñas Argon2id.
   - Configura la moneda principal del hogar (base ARS) y la regla de reparto.
   - Opcionalmente guarda la API Key de Google Gemini (cifrada con Fernet + HKDF).
   - Opcionalmente activa el módulo de Negocio/Freelance con nombre configurable.
   - Siembra las categorías universales y cuentas base.
5. Tras el alta, `/setup` queda deshabilitado para evitar reinicializaciones no autorizadas.

---

## 6. Sistema de Backups Cifrados y Rotación GFS

- El contenedor de backup genera volcados diarios a las 03:00 con `pg_dump`.
- El archivo se comprime y se cifra inmediatamente en reposo con **age** usando una clave pública (`AGE_PUBLIC_KEY`). La clave privada nunca debe residir en el servidor de producción.
- **Esquema de Rotación GFS (Grandfather-Father-Son):**
  - Diarios: 14 últimos backups.
  - Semanales: 8 últimos backups (domingos).
  - Mensuales: 12 últimos backups (día 1 de cada mes).
- Los backups pueden sincronizarse con Google Drive o Amazon S3 a través de `rclone`.
