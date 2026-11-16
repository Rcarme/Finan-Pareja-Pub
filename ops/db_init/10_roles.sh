#!/bin/sh
# Se ejecuta por la imagen PostgreSQL únicamente al inicializar un volumen vacío.
set +x
set -eu

reject() {
    printf '%s\n' "PROHIBIDO: $1" >&2
    exit 1
}

case "${POSTGRES_USER:-}" in
    ""|finanzas_app|finanzas_owner)
        reject "POSTGRES_USER debe ser un administrador distinto de los roles de la aplicación."
        ;;
esac

[ -n "${POSTGRES_PASSWORD:-}" ] || reject "Falta POSTGRES_PASSWORD."
[ -n "${POSTGRES_OWNER_PASSWORD:-}" ] || reject "Falta POSTGRES_OWNER_PASSWORD."
[ -n "${POSTGRES_APP_PASSWORD:-}" ] || reject "Falta POSTGRES_APP_PASSWORD."
[ "${#POSTGRES_PASSWORD}" -ge 12 ] || reject "POSTGRES_PASSWORD requiere al menos 12 caracteres."
[ "${#POSTGRES_OWNER_PASSWORD}" -ge 12 ] || reject "POSTGRES_OWNER_PASSWORD requiere al menos 12 caracteres."
[ "${#POSTGRES_APP_PASSWORD}" -ge 12 ] || reject "POSTGRES_APP_PASSWORD requiere al menos 12 caracteres."

for role_password in "$POSTGRES_PASSWORD" "$POSTGRES_OWNER_PASSWORD" "$POSTGRES_APP_PASSWORD"; do
    case "$role_password" in
        *CAMBIAR_PASSWORD*|*changeme*|*change-me*|*CHANGE_ME*|*replace-with*|*definir-password*|*definir-*|*password-admin*|*password-owner*|*password-app*|*admin123*|*secret*|*123456*)
            reject "Las contraseñas de bootstrap deben ser propias, no placeholders ni valores por defecto."
            ;;
    esac
done

if [ "$POSTGRES_PASSWORD" = "$POSTGRES_OWNER_PASSWORD" ] || \
   [ "$POSTGRES_PASSWORD" = "$POSTGRES_APP_PASSWORD" ] || \
   [ "$POSTGRES_OWNER_PASSWORD" = "$POSTGRES_APP_PASSWORD" ]; then
    reject "Las contraseñas de los 3 roles (admin, owner y app) deben ser distintas entre sí para garantizar el principio de menor privilegio."
fi

# getenv evita claves en argumentos/procesos y el quoting de psql evita interpolar SQL.
psql --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" <<'SQL'
\getenv owner_password POSTGRES_OWNER_PASSWORD
\getenv app_password POSTGRES_APP_PASSWORD
CREATE ROLE finanzas_owner WITH LOGIN PASSWORD :'owner_password' CREATEDB NOSUPERUSER NOCREATEROLE;
CREATE ROLE finanzas_app WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
SQL

psql --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --file=/opt/finanzas/db_roles.sql
