"""Rutas web para autenticación, login, logout y cambio de contraseña (F02)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.core.auth import (
    SESSION_DURATION_SECONDS,
    verify_password,
)
from app.core.db import get_db
from app.core.security import (
    clear_failed_attempts,
    generate_csrf_token,
    is_rate_limited,
    record_failed_attempt,
    secure_cookies,
    validate_csrf,
)
from app.models.auth import User
from app.services.auth_attempt_service import (
    complete_login,
    is_login_rate_limited,
    record_login_failure,
)
from app.services.auth_service import change_password, logout

auth_router = APIRouter()


@auth_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    """Muestra el formulario de inicio de sesión."""
    # Si ya tiene sesión activa, redirigir al inicio
    if getattr(request.state, "user", None):
        return RedirectResponse(url="/", status_code=303)

    csrf_token = generate_csrf_token()
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"title": "Iniciar sesión - Finanzas", "csrf_token": csrf_token},
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        samesite="lax",
        secure=secure_cookies(),
    )
    return response


@auth_router.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Procesa el formulario de inicio de sesión."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent")

    # 1. Validación CSRF (RN-38 y ARCHITECTURE § 4)
    if not validate_csrf(request, csrf_token):
        return JSONResponse(status_code=403, content={"detail": "Token CSRF inválido o ausente"})

    # 2. Control de intentos fallidos (Rate Limiting)
    if is_rate_limited(client_ip, username) or is_login_rate_limited(db, client_ip, username):
        return JSONResponse(
            status_code=429,
            content={"detail": "Demasiados intentos fallidos. Intente más tarde (15 min)."},
        )

    # 3. Búsqueda y validación del usuario (case-insensitive para celular)
    clean_username = username.strip().lower()
    stmt = select(User).where(func.lower(User.username) == clean_username, User.active == True)  # noqa: E712
    user = db.execute(stmt).scalar_one_or_none()

    # Si el usuario no existe, se calcula una verificación dummy contra timing
    dummy_hash = "$argon2id$v=19$m=65536,t=2,p=1$c29tZXNhbHQ$P6+2Sg71wV/l/kR+v1L1cQ"
    valid_password = (
        verify_password(password, user.password_hash)
        if user
        else (verify_password(password, dummy_hash) and False)
    )

    if not user or not valid_password:
        record_failed_attempt(client_ip, clean_username)
        record_login_failure(db, client_ip, clean_username)
        from app.web.app import templates

        new_csrf = generate_csrf_token()
        res = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "title": "Iniciar sesión - Finanzas",
                "error": "Usuario o contraseña incorrectos.",
                "csrf_token": new_csrf,
                "username": username,
            },
            status_code=401,
        )
        res.set_cookie(
            key="csrf_token",
            value=new_csrf,
            httponly=False,
            samesite="lax",
            secure=secure_cookies(),
        )
        return res

    # 4. Éxito: limpiar intentos y crear sesión
    raw_token = complete_login(db, client_ip, user.username, user.id, user_agent)
    clear_failed_attempts(client_ip, username)

    # Redirección con cookie HttpOnly segura
    redirect_res = RedirectResponse(url="/", status_code=303)
    redirect_res.set_cookie(
        key="session_id",
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=secure_cookies(),
        max_age=SESSION_DURATION_SECONDS,
    )
    return redirect_res


@auth_router.post("/logout")
def logout_action(
    request: Request,
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Cierra la sesión y revoca el token en BD."""
    if not validate_csrf(request, csrf_token):
        return JSONResponse(status_code=403, content={"detail": "Token CSRF inválido"})

    session_token = request.cookies.get("session_id")
    if session_token:
        logout(db, session_token)

    res = RedirectResponse(url="/login", status_code=303)
    res.delete_cookie(key="session_id")
    return res


@auth_router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request) -> Response:
    """Muestra el formulario para cambio de contraseña."""
    csrf_token = generate_csrf_token()
    from app.web.app import templates

    response = templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={
            "title": "Cambiar contraseña",
            "csrf_token": csrf_token,
            "user": getattr(request.state, "user", None),
        },
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        samesite="lax",
        secure=secure_cookies(),
    )
    return response


@auth_router.post("/change-password")
def change_password_action(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Procesa el cambio de contraseña."""
    if not validate_csrf(request, csrf_token):
        return JSONResponse(status_code=403, content={"detail": "Token CSRF inválido"})

    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    db_user = db.get(User, user.id)
    if not db_user:
        return RedirectResponse(url="/login", status_code=303)

    from app.web.app import templates

    new_csrf = generate_csrf_token()

    def render_error(msg: str) -> HTMLResponse:
        err_res = templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={
                "title": "Cambiar contraseña",
                "error": msg,
                "csrf_token": new_csrf,
                "user": db_user,
            },
            status_code=400,
        )
        err_res.set_cookie(
            key="csrf_token",
            value=new_csrf,
            httponly=False,
            samesite="lax",
            secure=secure_cookies(),
        )
        return err_res

    try:
        change_password(
            db,
            db_user.id,
            current_password,
            new_password,
            confirm_password,
            request.cookies.get("session_id"),
        )
    except ValueError as exc:
        return render_error(str(exc))

    return RedirectResponse(url="/?msg=password_updated", status_code=303)
