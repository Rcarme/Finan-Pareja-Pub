/* RN-39: instalación y estado de conexión compartidos por ambas vistas. */
(() => {
    const connection = document.createElement('p');
    connection.dataset.connectionStatus = '';
    connection.setAttribute('role', 'status');
    connection.setAttribute('aria-live', 'polite');
    document.body.prepend(connection);
    const updateConnection = () => {
        connection.hidden = navigator.onLine;
        connection.textContent = navigator.onLine ? '' : 'Sin conexión: no se pueden guardar cambios. Volvé a intentar cuando tengas conexión.';
    };
    window.addEventListener('online', updateConnection);
    window.addEventListener('offline', updateConnection);
    updateConnection();
    document.addEventListener('submit', (event) => {
        if (!navigator.onLine) {
            event.preventDefault();
            event.stopImmediatePropagation();
            updateConnection();
        }
    }, true);
    let promptEvent = null;
    const status = document.querySelector('[data-install-status]');
    const button = document.querySelector('[data-install-button]');
    const show = (message) => { if (status) status.textContent = message; };
    const installed = () => window.matchMedia('(display-mode: standalone)').matches;
    if (installed()) {
        show('La app ya está instalada.');
    } else if (!window.isSecureContext) {
        show('Para instalar, abrí la app por HTTPS con Tailscale. La dirección HTTP por IP permite usarla en Chrome y crear un acceso directo, pero no instalarla.');
    } else if (!('serviceWorker' in navigator)) {
        show('Este navegador no permite instalar la app. Podés crear un acceso directo desde su menú.');
    } else {
        show('Verificando instalación… Si tu navegador no ofrece instalar, podés crear un acceso directo desde su menú.');
    }
    if (window.isSecureContext && 'serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' }).then(() => {
            if (!installed() && !promptEvent) show('Abrí el menú del navegador y elegí Instalar app o Agregar a pantalla principal. Si solo aparece acceso directo, el navegador todavía no habilitó la instalación.');
        }).catch(() => {
            show('No se pudo preparar la instalación. Verificá la conexión y recargá la página. Podés seguir usando la app en el navegador.');
        });
    }
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        promptEvent = event;
        if (button) button.hidden = false;
        show('La app está lista para instalar.');
    });
    if (button) button.addEventListener('click', async () => {
        if (!promptEvent) return;
        const current = promptEvent;
        promptEvent = null;
        button.hidden = true;
        try {
            await current.prompt();
            const choice = await current.userChoice;
            show(choice.outcome === 'accepted' ? 'Instalación solicitada. El navegador te avisará cuando termine.' : 'Instalación cancelada. Podés intentarlo desde el menú del navegador.');
        } catch (_) {
            show('No se pudo iniciar la instalación. Intentá desde el menú del navegador.');
        }
    });
    window.addEventListener('appinstalled', () => {
        promptEvent = null;
        if (button) button.hidden = true;
        show('La app está instalada.');
    });
})();
