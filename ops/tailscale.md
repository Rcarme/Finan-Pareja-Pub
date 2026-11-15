# Guía Operativa: HTTPS y PWA con Tailscale (`ops/tailscale.md`)

Esta guía describe cómo desplegar y acceder a la aplicación **Finanzas en Común** de forma segura en la red privada de la pareja (tailnet) utilizando HTTPS válido emitido por Tailscale, permitiendo la instalación completa como Progressive Web App (PWA) en celulares Android e iOS.

---

## 1. Principios de Seguridad y Red

1. **Sin exposición a Internet público:**
   - La aplicación **NUNCA** debe escuchar en `0.0.0.0` accesible desde Internet.
   - En el host/servidor, el servicio web escucha exclusivamente en loopback (`127.0.0.1:8088`) o en la IP privada asignada por Tailscale (`100.x.y.z`).
   
2. **Cifrado punto a punto y certificados válidos:**
   - Para registrar un **Service Worker** e instalar una **PWA**, los navegadores modernos (Chrome en Android, Safari en iOS) exigen obligatoriamente una conexión segura **HTTPS** con certificado TLS reconocido.
   - `tailscale serve` proporciona certificados TLS legítimos de Let's Encrypt para el dominio propio de la máquina dentro de la tailnet (`https://<nombre-nodo>.<tailnet>.ts.net`).

---

## 2. Configuración en el Servidor

### Requisitos previos
- Tailscale instalado en el host (`tailscale version`).
- Tailscale autenticado en la cuenta compartida de la pareja (`tailscale up`).
- "MagicDNS" y "HTTPS Certificates" habilitados en el panel de control de Tailscale (Admin Console -> DNS -> Enable HTTPS Certificates).

### Paso 1: Iniciar la aplicación en loopback
Asegurarse de que el servidor FastAPI corra en `127.0.0.1`:
```bash
# En desarrollo / pruebas (8088 en el host; Docker mantiene 8000 interno):
.venv/bin/uvicorn app.web.app:app --host 127.0.0.1 --port 8088

# En producción con Docker Compose:
# En docker-compose.yml, mapear puertos a loopback:
# ports:
#   - "127.0.0.1:8088:8000"
```

Verificar que el puerto 8088 solo esté abierto en loopback:
```bash
# En Linux:
ss -tulpn | grep 8088
# Debe mostrar 127.0.0.1:8088, NUNCA 0.0.0.0:8088

# En Windows:
netstat -ano | findstr 8088
# Debe mostrar 127.0.0.1:8088
```

### Paso 2: Publicar HTTPS en la Tailnet con `tailscale serve`
Ejecutar el comando de proxy inverso integrado de Tailscale:
```bash
tailscale serve --bg 8088
```

Para verificar el estado y la URL asignada:
```bash
tailscale serve status
```
La salida mostrará una URL similar a:
```text
https://servidor-finanzas.tailnet-xyz.ts.net (proxying to http://127.0.0.1:8088)
```

Tailscale se encarga de:
- Escuchar en el puerto 443 de la interfaz de Tailscale.
- Terminar TLS con certificado válido de Let's Encrypt.
- Reenviar las peticiones de forma transparente a `http://127.0.0.1:8088`.

---

## 3. Instalación de la PWA en Celulares

Ambos miembros de la pareja deben tener la aplicación de Tailscale instalada y conectada en sus teléfonos móviles.

### A. En Android (Google Chrome)
1. Abrir la app **Tailscale** en el teléfono y verificar que esté conectada.
2. Abrir **Google Chrome** e ingresar a la URL HTTPS: `https://<nodo>.<tailnet>.ts.net`.
3. Iniciar sesión con su usuario y contraseña.
4. Tocar el menú de opciones (los tres puntos verticales `⋮` arriba a la derecha).
5. Seleccionar **"Instalar aplicación"** (o "Agregar a la pantalla principal").
6. Confirmar la instalación.
7. La aplicación aparecerá en el cajón de aplicaciones y en la pantalla de inicio con su icono y nombre **Finanzas**, abriéndose en pantalla completa (`display: standalone`) sin barras del navegador.

### B. En iOS (iPhone - Safari)
1. Abrir la app **Tailscale** en el iPhone y verificar que la VPN esté conectada.
2. Abrir **Safari** (obligatorio en iOS para instalar PWAs) e ingresar a la URL HTTPS: `https://<nodo>.<tailnet>.ts.net`.
3. Iniciar sesión con su usuario y contraseña.
4. Tocar el botón de **Compartir** (el ícono del cuadrado con la flecha hacia arriba en la barra inferior).
5. En el menú de acciones, desplazarse hacia abajo y seleccionar **"Agregar a pantalla de inicio"** (*Add to Home Screen*).
6. Verificar el nombre "Finanzas" y tocar **"Agregar"**.
7. La app se guardará en la pantalla de inicio como una aplicación nativa independiente.

---

## 4. Verificación del Comportamiento Offline (RN-39)

La aplicación implementa la regla contable **RN-39**: *No existe modo offline para escritura. Si no hay conexión, la UI lo informa y no guarda nada*.

### Prueba de desconexión en el dispositivo:
1. Con la PWA abierta en el teléfono, activar el "Modo Avión" o apagar WiFi y datos móviles.
2. Inmediatamente se mostrará en la parte superior el banner rojo:
   > ⚠️ **Sin conexión a internet. No se pueden registrar movimientos ni modificar datos contables (RN-39).**
3. Los botones de formulario (`+ Gasto`, `Guardar`, `Confirmar`) se deshabilitan automáticamente para impedir cargas que puedan generar inconsistencias o duplicados.
4. Si se intenta forzar el envío, la aplicación intercepta el evento y alerta al usuario.
5. Al desactivar el Modo Avión y restablecer la conexión, el banner desaparece y los controles vuelven a habilitarse automáticamente sin pérdida de datos contables previos.
