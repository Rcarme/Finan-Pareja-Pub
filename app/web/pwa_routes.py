"""Rutas web para PWA: manifiesto, service worker y página offline (F09, RN-39)."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

pwa_router = APIRouter()
STATIC_DIR = Path(__file__).parent / "static"


@pwa_router.get("/manifest.webmanifest")
def manifest_webmanifest() -> Response:
    """Devuelve el manifiesto PWA con Content-Type application/manifest+json."""
    manifest_file = STATIC_DIR / "manifest.webmanifest"
    return FileResponse(
        path=manifest_file,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@pwa_router.get("/manifest.json")
def manifest_json() -> Response:
    """Alias compatible para clientes o navegadores que solicitan /manifest.json."""
    return manifest_webmanifest()


@pwa_router.get("/sw.js")
def service_worker() -> Response:
    """Devuelve el Service Worker con ámbito raíz (Service-Worker-Allowed: /)."""
    sw_file = STATIC_DIR / "js" / "sw.js"
    return FileResponse(
        path=sw_file,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@pwa_router.get("/offline", response_class=HTMLResponse)
def offline_page(request: Request) -> Response:
    """Página de contingencia offline según RN-39."""
    from app.web.app import templates

    return templates.TemplateResponse(
        request=request,
        name="offline.html",
        context={"title": "Sin conexión (RN-39) - Tandem"},
    )
