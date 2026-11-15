# Operación de base de datos — roles y migraciones (F05.2 N5)

## 1. Provisionar roles (lo ejecuta el humano con un rol administrador)

`ops/db_roles.sql` aplica privilegios, es idempotente y aborta si no existen ambos
roles. No crea logins con claves conocidas. En un volumen Docker nuevo el
bootstrap `ops/db_init/10_roles.sh` los crea usando las tres claves privadas del
entorno. En PostgreSQL externo, crearlos primero desde una consola administrativa
sin publicar las claves en scripts ni en el historial de comandos. Roles mínimos:

- `finanzas_owner`: LOGIN, CREATEDB, NOSUPERUSER, NOCREATEROLE.
- `finanzas_app`: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOINHERIT.

Luego aplicar el SQL en **cada base**:

```bash
psql -U <admin> -d finanzas -f ops/db_roles.sql
psql -U <admin> -d finanzas_test -f ops/db_roles.sql
```

Roles requeridos:

| Rol | Uso | Permisos |
|---|---|---|
| `finanzas_owner` | Migraciones DDL (`MIGRATION_DATABASE_URL`) | Dueño del esquema, DDL completa |
| `finanzas_app` | App web y servicios (`DATABASE_URL`) | SELECT/INSERT/UPDATE en negocio; solo SELECT+INSERT en `audit_log`; DELETE solo en `sessions`. Sin TRUNCATE, sin DELETE contable, sin superuser |

Las claves se eligen fuera del SQL y se custodian en el gestor de contraseñas.
Este procedimiento no rota claves de roles existentes.

## 2. URLs por rol (`.env`)

```bash
# Rol dueño: solo para migraciones
MIGRATION_DATABASE_URL=postgresql://finanzas_owner:<pass>@<host>:5432/finanzas
# Rol app: uso diario (web + servicios)
DATABASE_URL=postgresql://finanzas_app:<pass>@<host>:5432/finanzas
TEST_DATABASE_URL=postgresql://finanzas_app:<pass>@<host>:5432/finanzas_test
```

Si `MIGRATION_DATABASE_URL` no está definida, `alembic` usa `DATABASE_URL`
(comportamiento anterior, útil en desarrollo).

## 3. Verificación (la hace el auditor, no el humano)

`tests/integration/test_db_roles_script.py` ejecuta `ops/db_roles.sql` **verbatim**
en una base descartable y verifica `rolsuper=false`, sin TRUNCATE y sin DELETE
en tablas contables.
