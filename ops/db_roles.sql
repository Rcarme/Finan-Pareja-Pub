-- ops/db_roles.sql
-- Roles con privilegios mínimos para la app Finanzas (F05.1, F05.2 N5, F13 C-04).
-- SQL puro, sin metacomandos de psql: se puede ejecutar tal cual con cualquier
-- cliente (psql, psycopg, DBeaver), tanto ANTES como DESPUÉS de correr las
-- migraciones de Alembic.
-- Ejecutarlo como superusuario/rol administrador en CADA base (`finanzas` y `finanzas_test`):
--   psql -U <admin> -d <base> -f ops/db_roles.sql
-- Ver ops/README.md y docs/RUNBOOK.md para el procedimiento completo.

-- 1. Los roles se provisionan antes con credenciales privadas, nunca con placeholders.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finanzas_owner') THEN
        RAISE EXCEPTION 'Falta finanzas_owner: provisionar credenciales privadas antes de aplicar privilegios';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finanzas_app') THEN
        RAISE EXCEPTION 'Falta finanzas_app: provisionar credenciales privadas antes de aplicar privilegios';
    END IF;
END $$;

-- 2. Ownership de tablas/secuencias para finanzas_owner (migraciones DDL);
-- El schema public se mantiene bajo ownership de CURRENT_USER (admin) para imposibilitar DROP SCHEMA CASCADE (H-04).
DO $$
BEGIN
    EXECUTE format('ALTER SCHEMA public OWNER TO %I', CURRENT_USER);
END $$;
GRANT USAGE, CREATE ON SCHEMA public TO finanzas_owner;

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO finanzas_owner', r.tablename);
    END LOOP;
    FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO finanzas_owner', r.sequencename);
    END LOOP;
END $$;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO finanzas_owner;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO finanzas_owner;

-- 3. Defaults para objetos futuros creados por finanzas_owner
ALTER DEFAULT PRIVILEGES FOR ROLE finanzas_owner IN SCHEMA public
    GRANT ALL ON TABLES TO finanzas_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE finanzas_owner IN SCHEMA public
    GRANT ALL ON SEQUENCES TO finanzas_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE finanzas_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO finanzas_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finanzas_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO finanzas_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finanzas_owner IN SCHEMA public
    REVOKE DELETE, TRUNCATE ON TABLES FROM finanzas_app;

-- 4. Conexión y uso de esquema para el rol de aplicación (sin CREATE ni DROP)
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO finanzas_app', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO finanzas_app;
REVOKE CREATE ON SCHEMA public FROM finanzas_app;

-- 5. Función de privilegios mínimos por tabla (cubre todas las tablas de Base.metadata)
-- Se ejecuta inmediatamente sobre las tablas existentes y también mediante un EVENT TRIGGER
-- cuando finanzas_owner crea tablas durante `alembic upgrade head`.
--
-- Matriz explícita de tablas cubiertas:
--   GRANT SELECT, INSERT, UPDATE ON TABLE
--       accounts, categories, transactions, envelopes, envelope_movements,
--       obligations, obligation_items, periods, closes, carryovers, fx_rates,
--       recurring_templates, recurring_pending, rent_contract, rent_adjustments,
--       settings, users
--   TO finanzas_app;
--   GRANT SELECT, INSERT ON TABLE audit_log TO finanzas_app;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sessions, idempotency_keys TO finanzas_app;
--   GRANT SELECT ON TABLE alembic_version TO finanzas_app;

CREATE OR REPLACE FUNCTION public.finanzas_apply_role_privileges()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    tbl TEXT;
    rw_tables TEXT[] := ARRAY[
        'accounts',
        'categories',
        'transactions',
        'card_statements',
        'merchant_rules',
        'envelopes',
        'envelope_movements',
        'obligations',
        'obligation_items',
        'periods',
        'closes',
        'carryovers',
        'fx_rates',
        'recurring_templates',
        'recurring_pending',
        'rent_contract',
        'rent_adjustments',
        'settings',
        'users'
    ];
    del_tables TEXT[] := ARRAY[
        'sessions',
        'idempotency_keys'
    ];
BEGIN
    FOREACH tbl IN ARRAY rw_tables LOOP
        IF to_regclass('public.' || tbl) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO finanzas_app', tbl);
            EXECUTE format('REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.%I FROM finanzas_app', tbl);
        END IF;
    END LOOP;

    -- Auditoría (RN-37): estrictamente append-only (SELECT, INSERT; sin UPDATE ni DELETE ni TRUNCATE)
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        -- Separación de privilegios: aislar ownership de audit_log de finanzas_owner para que
        -- el rol de migraciones no pueda desactivar triggers de inmutabilidad (H-06).
        EXECUTE format('ALTER TABLE public.audit_log OWNER TO %I', CURRENT_USER);
        GRANT SELECT, INSERT ON TABLE public.audit_log TO finanzas_owner;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.audit_log FROM finanzas_owner;

        GRANT SELECT, INSERT ON TABLE public.audit_log TO finanzas_app;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.audit_log FROM finanzas_app;

        -- Trigger de defensa en profundidad: UPDATE, DELETE o TRUNCATE arroja excepción en el motor
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION trg_audit_log_immutable()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'audit_log es estrictamente append-only: UPDATE, DELETE y TRUNCATE están prohibidos.';
            END;
            $$ LANGUAGE plpgsql;
        $fn$;

        DROP TRIGGER IF EXISTS trg_audit_log_protect ON public.audit_log;
        DROP TRIGGER IF EXISTS trg_audit_log_protect_row ON public.audit_log;
        CREATE TRIGGER trg_audit_log_protect_row
        BEFORE UPDATE OR DELETE ON public.audit_log
        FOR EACH ROW EXECUTE FUNCTION trg_audit_log_immutable();

        DROP TRIGGER IF EXISTS trg_audit_log_protect_truncate ON public.audit_log;
        CREATE TRIGGER trg_audit_log_protect_truncate
        BEFORE TRUNCATE ON public.audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_log_immutable();
    END IF;

    -- Pertenencia del dinero: correcciones por contrapartida, nunca mutación del asiento.
    IF to_regclass('public.ownership_entries') IS NOT NULL THEN
        GRANT SELECT, INSERT ON TABLE public.ownership_entries TO finanzas_app;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.ownership_entries FROM finanzas_app;
    END IF;

    -- Historia externa de pólizas: solo inserciones explícitas, nunca cambios o borrados.
    IF to_regclass('public.retirement_contributions') IS NOT NULL THEN
        GRANT SELECT, INSERT ON TABLE public.retirement_contributions TO finanzas_app;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.retirement_contributions FROM finanzas_app;
    END IF;

    -- Sesiones y claves de idempotencia: requieren DELETE (logout/expiración y abort_operation)
    FOREACH tbl IN ARRAY del_tables LOOP
        IF to_regclass('public.' || tbl) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO finanzas_app', tbl);
            EXECUTE format('REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE public.%I FROM finanzas_app', tbl);
        END IF;
    END LOOP;

    -- Versión de Alembic: solo lectura para finanzas_app
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        GRANT SELECT ON TABLE public.alembic_version TO finanzas_app;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.alembic_version FROM finanzas_app;
    END IF;

    -- Secuencias para IDs autoincrementales
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finanzas_app;
END;
$$;

CREATE OR REPLACE FUNCTION public.finanzas_ddl_privilege_trigger_fn()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
BEGIN
    PERFORM public.finanzas_apply_role_privileges();
END;
$$;

DROP EVENT TRIGGER IF EXISTS trg_finanzas_ddl_privileges;
CREATE EVENT TRIGGER trg_finanzas_ddl_privileges
    ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'CREATE SEQUENCE', 'ALTER TABLE')
    EXECUTE FUNCTION public.finanzas_ddl_privilege_trigger_fn();

-- 6. Aplicar inmediatamente sobre cualquier tabla/secuencia que ya exista en la base
SELECT public.finanzas_apply_role_privileges();
