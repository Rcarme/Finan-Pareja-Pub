# Guía Operativa: Google Drive con Rclone y Cifrado Age (`ops/rclone-gdrive.md`)

Esta guía detalla la configuración de copias de seguridad remotas en **Google Drive** mediante `rclone` y el esquema de cifrado asimétrico con `age`, garantizando que **Drive nunca reciba datos en texto plano** y que la clave privada de restauración permanezca bajo estricta custodia fuera del servidor (ARCHITECTURE.md §6).

---

## 1. Esquema de Cifrado con `age`

El cifrado asimétrico permite que el servidor genere copias de seguridad seguras sin necesidad de poseer la capacidad de descifrarlas:

- **Clave Pública (`age1...`):** Reside en el servidor (variable `AGE_PUBLIC_KEY` en `.env`). Se utiliza a diario para cifrar los volcados de base de datos (`pg_dump`).
- **Clave Privada (`AGE-SECRET-KEY-1...`):** **NUNCA** debe guardarse en el servidor de producción. Debe custodiarse en:
  1. El gestor de contraseñas de la pareja (Bitwarden / 1Password).
  2. Una copia de seguridad física impresa almacenada de forma segura.

### Generación de llaves:
En una máquina segura (de desarrollo o personal):
```bash
# Instalar age si no está disponible (ej: brew install age / apt install age / winget install age)
age-keygen -o key.txt
```
Salida típica:
```text
# created: 2026-10-05T12:00:00Z
# public key: age1<CLAVE_PUBLICA_FICTICIA_DE_EJEMPLO_NO_USAR>
AGE-SECRET-KEY-1...
```
1. Copiar la línea `public key: age1...` al `.env` del servidor como `AGE_PUBLIC_KEY=age1...`.
2. Guardar el archivo `key.txt` completo en el gestor de contraseñas.
3. Eliminar el archivo de la máquina donde se generó.

---

## 2. Configuración de Rclone para Google Drive

`rclone` permite sincronizar archivos con Google Drive a través de su API oficial mediante OAuth 2.0.

### Paso 1: Configurar el remote en rclone
En el servidor o máquina donde corre el contenedor de backup:
```bash
rclone config --config ops/rclone-config/rclone.conf
```
1. `n) New remote`
2. `name> gdrive`
3. `Storage> drive` (Google Drive)
4. `client_id>` (dejar en blanco para cliente predeterminado, o usar ID propio de Google Cloud)
5. `client_secret>` (dejar en blanco)
6. `scope> 1` (acceso completo a Drive para lectura y escritura)
7. `root_folder_id>`: ID de la carpeta creada en Google Drive para los backups (ej: `finanzas_backups`).
8. `service_account_file>`: dejar en blanco si se usa autenticación web.
9. `Edit advanced config?`: `n`
10. `Use auto config?`: `y` (si hay navegador en la máquina) o `n` (para autorizar desde otra máquina con `rclone authorize "drive"`).
11. Seguir el enlace de inicio de sesión de Google, conceder permisos y pegar el código de verificación.
12. `Keep this "gdrive" remote?`: `y`

### Paso 2: Verificar la conexión
```bash
rclone --config ops/rclone-config/rclone.conf lsd gdrive:
```
Si devuelve la lista de directorios, la conexión está lista.

---

## 3. Automatización de Copias

El contenedor monta `ops/rclone-config` como `/etc/rclone` en solo lectura y usa `RCLONE_CONFIG=/etc/rclone/rclone.conf`. La configuración privada queda fuera de Git y de la imagen; debe poder leerla `appuser`. El contenedor ejecuta cada día desde las 03:00 de Argentina, con recuperación al arrancar tarde, el script `ops/backup/backup.sh`, el cual:
1. Exporta la base de datos con `pg_dump -Fc`.
2. Cifra el volcado con la clave pública: `age -r "$AGE_PUBLIC_KEY" -o finanzas_YYYYMMDD.dump.age finanzas_YYYYMMDD.dump`.
3. Elimina inmediatamente el dump temporal no cifrado.
4. Sube el archivo cifrado a Google Drive: `rclone copy finanzas_YYYYMMDD.dump.age gdrive:finanzas_backups/daily/`.
5. Aplica la política de retención:
   - **Diarios:** 14 copias.
   - **Semanales:** 8 copias.
   - **Mensuales:** 12 copias.

Si falta configuración, conserva la copia cifrada local, registra destino `local` y una advertencia. Si falla una subida, conserva local y alerta crítica. No informa `drive` hasta completar las subidas previstas. La verificación liviana dominical compara archivos cifrados y no equivale a restaurarlos; la prueba completa necesita identidad privada externa y credenciales de verificación.

---

## 4. Verificación de Cifrado (No legibilidad en Drive)

Para verificar que los datos están adecuadamente protegidos:
1. Descargar uno de los archivos `.dump.age` desde la interfaz web de Google Drive.
2. Abrirlo con un editor de texto o visor hexadecimal.
3. El encabezado del archivo comenzará con:
   ```text
   age-encryption.org/v1
   -> X25519 ...
   ```
   seguido de bytes pseudoaleatorios cifrados con ChaCha20-Poly1305.
4. Intentar restaurarlo con `pg_restore` sin descifrar fallará inmediatamente con: `pg_restore: error: did not find magic header`.
5. Esto garantiza que ante una brecha de seguridad en Google Drive, **ningún dato financiero familiar pueda ser leído**.
