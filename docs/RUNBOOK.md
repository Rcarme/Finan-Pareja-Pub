# Manual de Operaciones y Mantenimiento (RUNBOOK.md)

Instrucciones paso a paso para el despliegue, mantenimiento, backups y recuperación ante desastres de **Finanzas en Pareja**.

---

## 1. Despliegue Inicial con Docker Compose

### 1.1. Prerrequisitos
- Servidor Linux / Homelab / VPS con Docker y Docker Compose instalados.
- Puertos disponibles: `8088` (o el que configures en `APP_HOST_PORT`).

### 1.2. Paso a Paso
1. Clonar el repositorio:
   ```bash
   git clone https://github.com/Rcarme/Finan-Pareja-Pub.git
   cd Finan-Pareja-Pub
   ```
2. Generar el archivo `.env`:
   ```bash
   cp .env.example .env
   ```
3. Generar una clave secreta segura para la app:
   ```bash
   openssl rand -hex 32
   ```
   Copiá el valor en `APP_SECRET_KEY` dentro de `.env`.
4. Definir contraseñas seguras para los tres roles de PostgreSQL:
   - `POSTGRES_ADMIN_PASSWORD`
   - `POSTGRES_OWNER_PASSWORD`
   - `POSTGRES_APP_PASSWORD`
5. Iniciar la aplicación:
   ```bash
   docker compose up -d
   ```
6. Ingresar desde el navegador a `http://TU_IP:8088` para completar el asistente inicial (`/setup`).

---

## 2. Configuración de Backups Cifrados con age

### 2.1. Generar Par de Claves age
En tu máquina personal segura (NO en el servidor de producción):
```bash
# Instalar age si no lo tenés (ej. brew install age, apt install age, choco install age)
age-keygen -o key.txt
```
El archivo `key.txt` contiene:
- `public key: age1...` (Esta es tu `AGE_PUBLIC_KEY`).
- Clave privada secreta (Custodiala en tu gestor de contraseñas personal).

Copiá la clave pública en `AGE_PUBLIC_KEY=` de tu archivo `.env`.

### 2.2. Configuración de rclone para Almacenamiento Remoto (Opcional)
Para sincronizar automáticamente tus copias de seguridad en Google Drive o S3:
1. Configurá un remote con `rclone config` en tu máquina y generá un archivo `rclone.conf`.
2. Copiá `rclone.conf` en la carpeta `ops/rclone-config/rclone.conf`.
3. Definí en `.env`:
   ```env
   RCLONE_REMOTE=gdrive:finanzas_backups
   ```

---

## 3. Procedimiento de Restauración (Disaster Recovery)

Si necesitás restaurar la base de datos a partir de una copia de seguridad cifrada:

1. Obtené el archivo de backup cifrado más reciente (ejemplo: `finanzas_20261115_030000.sql.gz.age`).
2. Descifralo en una máquina segura con tu clave privada:
   ```bash
   age -d -i key.txt finanzas_20261115_030000.sql.gz.age > finanzas_restored.sql.gz
   gunzip finanzas_restored.sql.gz
   ```
3. Restaurá el archivo `.sql` en el contenedor de base de datos:
   ```bash
   docker compose exec -T db psql -U finanzas_bootstrap -d finanzas < finanzas_restored.sql
   ```
4. Reiniciá el servicio de la aplicación:
   ```bash
   docker compose restart app
   ```
