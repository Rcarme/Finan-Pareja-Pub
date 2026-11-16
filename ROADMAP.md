# Hoja de Ruta (Roadmap) - Finanzas en Pareja

Este documento resume las iniciativas, mejoras arquitectónicas y funcionalidades planificadas para las próximas versiones de **Finanzas en Pareja**.

---

## 🧭 Visión del Proyecto
Brindar una plataforma de finanzas colaborativas doméstica, privada, libre de telemetría y auditada, con garantías formales de integridad contable y control total sobre los datos propios.

---

## 🎯 Corto Plazo (v1.1)

- [ ] **Suite de Pruebas de Integración con PostgreSQL en CI:**
  - Configurar un servicio de PostgreSQL (`services: postgres`) en GitHub Actions para validar contra el motor real de producción.
  - Ejecutar tests de concurrencia y validación de triggers (`BEFORE UPDATE OR DELETE` y `BEFORE TRUNCATE` en `audit_log`).
- [ ] **Depuración de Métricas de Rendimiento:**
  - Optimizar índices para consultas agregadas mensuales en cuentas con más de 10.000 transacciones históricas.
  - Paginación dinámica en las tablas de auditoría y movimientos históricos.
- [ ] **Refinamiento de UI Móvil:**
  - Pulir accesibilidad de botones de acción rápida en pantallas pequeñas (PWA / navegadores móviles).
  - Feedback háptico opcional para entrada por teclado numérico.

---

## 🚀 Mediano Plazo (v1.2 - v1.5)

- [ ] **Arquitectura Multidivisa Completa (Multi-Currency Ledger):**
  - Configuración de moneda base por hogar (ARS, USD, EUR, etc.).
  - Registro de libro mayor con cotizaciones históricas por fecha de transacción.
  - Resumen patrimonial consolidado con conversión automática a moneda base y cálculo de ganancia/pérdida cambiaria no realizada.
- [ ] **Autenticación en Dos Pasos Opcional (2FA / TOTP):**
  - Implementación de TOTP (RFC 6238) mediante aplicaciones autenticadoras (Google Authenticator, Aegis, 1Password, etc.).
  - Códigos de recuperación de emergencia de un solo uso cifrados con Fernet.
- [ ] **Internacionalización y Localización (i18n / l10n):**
  - Extracción de cadenas mediante `gettext`.
  - Soporte inicial para Español (`es`), Inglés (`en`) y Portugués (`pt`).
  - Formato regional de fechas y separadores numéricos configurables por usuario.

---

## 🌐 Largo Plazo (v2.0)

- [ ] **API de Sincronización y Webhooks Domésticos:**
  - API de solo lectura y endpoints autenticados con tokens de scoped access para integración con Home Assistant.
  - Disparadores de webhooks en caso de cierres mensuales o desbalances significativos en sobres virtuales.
- [ ] **Exportación Contable Avanzada:**
  - Generación de reportes periódicos en formatos estándar abiertos (OFX, QIF, CSV contable estandarizado).
  - Balance de sumas y saldos descargable en PDF firmado.
- [ ] **Conciliación Bancaria Semi-Automatizada:**
  - Comparación de extractos bancarios subidos en CSV contra registros pendientes.
  - Detección asistida por reglas heurísticas y ML local para emparejamiento inteligente de transferencias.

---

## 🤝 Cómo Contribuir
¿Te interesa liderar o colaborar en alguna de estas iniciativas? Consultá nuestra [Guía de Contribución](CONTRIBUTING.md) o abrí una propuesta en los [GitHub Discussions / Issues](https://github.com/Rcarme/Finan-Pareja-Pub/issues).
