# Configuración privada de rclone

Guardá `rclone.conf` en esta carpeta únicamente en el servidor. Compose la monta
como `/etc/rclone` en solo lectura, exclusivamente para el worker de backup.
El archivo contiene credenciales: no lo subas a Git, no lo incluyas en la imagen.
Configurá el remoto con `rclone config --config ops/rclone-config/rclone.conf`.
El directorio debe ser legible por `appuser` en el contenedor.

Sin configuración se conserva el backup cifrado local y aparece una advertencia;
no se informa una copia en Drive ni una restauración exitosa.
