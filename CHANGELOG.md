# Changelog

Todas las modificaciones notables a este proyecto serán documentadas en este archivo.
El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/) y este proyecto adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [1.0.0] - 2026-11-15

### Agregado
- **Asistente de Configuración Inicial (/setup):**
  - Configuración paso a paso en primer inicio sin intervención manual en base de datos.
  - Bloqueo transaccional de concurrencia mediante PostgreSQL Advisory Lock para prevenir carreras de inicialización.
  - Soporte de token previo opcional (`SETUP_TOKEN`) para proteger instancias con puertos públicos antes del onboarding.
- **Seguridad y Criptografía:**
  - Hashing de contraseñas con **Argon2id** (mínimo 12 caracteres).
  - Cifrado simétrico de credenciales y secretos (ej. Gemini API Key) con **Fernet (AES-128-CBC + HMAC-SHA256)** derivado mediante **HKDF-SHA256**.
  - Protección estricta contra falsificación de IP (`request.client.host` y `FORWARDED_ALLOW_IPS` configurable).
  - Rate limiting dual contra ataques de fuerza bruta (por IP+usuario y global por usuario contra ataques distribuidos).
  - Protección CSRF universal con token doble (Cookie + Formulario/Encabezado HTMX `X-CSRF-Token`).
  - Cabeceras de seguridad estrictas: CSP con nonce dinámico, HSTS (`max-age=63072000`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Permissions-Policy`.
  - Tablas de auditoría inmutables (`audit_log`) con triggers en base de datos que bloquean `UPDATE`, `DELETE` y `TRUNCATE`.
- **Arquitectura y Contabilidad:**
  - Modelo de partida doble y conservación de fondos con `Decimal` (cero tolerancia a coma flotante).
  - Control de concurrencia optimista (`version: int`) en cuentas, categorías y transacciones.
  - Cierre mensual determinista con arrastre de saldos y cálculo proporcional de aportes.
  - Sobres virtuales de ahorro, metas y fondo de emergencia.
  - Seguimiento de tarjetas de crédito con corte, vencimiento e importación de resúmenes.
- **Asistente con Inteligencia Artificial:**
  - Integración opcional con Google Gemini para procesamiento en lenguaje natural y dictado por voz.
  - Detección inteligente de duplicados con normalización semántica en ventana temporal de 90 días.
  - Verificación de contexto seguro (`window.isSecureContext`) para la API de reconocimiento de voz.
- **Gobernanza Open Source:**
  - Licencia MIT.
  - `ARCHITECTURE.md` y `docs/RUNBOOK.md` con manual operativo integral.
  - Documentación de seguridad (`SECURITY.md`), guía de contribución (`CONTRIBUTING.md`) y código de conducta (`CODE_OF_CONDUCT.md`).
  - Automatización de integración continua (CI) en GitHub Actions con `ruff`, `mypy --strict`, escáner de secretos `Gitleaks` y suite completa de tests automatizados.
