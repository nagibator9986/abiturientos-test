"""Безопасность: CSRF-защита, доступ в админку, защита от подбора пароля."""
from __future__ import annotations

import hmac
import secrets
import time
from functools import wraps

from flask import abort, current_app, flash, g, jsonify, redirect, request, session, url_for

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
ADMIN_SESSION_KEY = "admin_user_id"

# Счётчик неудачных входов: {ip: (количество, время_блокировки)}
_login_attempts: dict[str, tuple[int, float]] = {}
_LOGIN_ATTEMPTS_LIMIT = 512          # верхняя граница, чтобы словарь не рос бесконечно
_LOGIN_ATTEMPTS_TTL = 3600           # запись живёт не дольше часа


def generate_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    provided = request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER, "")
    if not provided and request.is_json:
        provided = (request.get_json(silent=True) or {}).get(CSRF_FORM_FIELD, "")
    return hmac.compare_digest(str(expected), str(provided))


def csrf_protect() -> None:
    """before_request-хук: любой изменяющий запрос обязан нести валидный токен."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if not validate_csrf():
        if request.is_json or request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "csrf", "message": "Сессия устарела. Обновите страницу."}), 400
        abort(400, description="Недействительный CSRF-токен. Обновите страницу и повторите.")
    return None


def client_ip() -> str:
    """IP клиента. X-Forwarded-For учитывается только при TRUST_PROXY=1,
    иначе заголовок можно подделать и обойти блокировку входа."""
    if current_app.config.get("TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def login_locked_seconds(ip: str) -> int:
    count, until = _login_attempts.get(ip, (0, 0.0))
    remaining = int(until - time.time())
    return remaining if remaining > 0 else 0


def _evict_login_attempts(now: float) -> None:
    """Чистит просроченные записи и не даёт словарю расти бесконечно."""
    stale = [ip for ip, (_c, until) in _login_attempts.items() if until and until < now - _LOGIN_ATTEMPTS_TTL]
    for ip in stale:
        _login_attempts.pop(ip, None)
    if len(_login_attempts) > _LOGIN_ATTEMPTS_LIMIT:
        # оставляем самые «свежие» блокировки
        for ip, _ in sorted(_login_attempts.items(), key=lambda kv: kv[1][1])[: len(_login_attempts) // 2]:
            _login_attempts.pop(ip, None)


def register_failed_login(ip: str) -> None:
    max_attempts = current_app.config.get("LOGIN_MAX_ATTEMPTS", 8)
    lockout = current_app.config.get("LOGIN_LOCKOUT_SECONDS", 300)
    now = time.time()
    _evict_login_attempts(now)
    count, until = _login_attempts.get(ip, (0, 0.0))
    if until and until < now:
        count = 0
    count += 1
    _login_attempts[ip] = (count, now + lockout if count >= max_attempts else 0.0)


def reset_login_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)


def current_admin():
    from .models import AdminUser  # локальный импорт: избегаем цикла

    admin_id = session.get(ADMIN_SESSION_KEY)
    if not admin_id:
        return None
    if getattr(g, "_admin_cache", None) is None:
        from .extensions import db

        g._admin_cache = db.session.get(AdminUser, admin_id)
    return g._admin_cache


def login_admin(admin) -> None:
    session[ADMIN_SESSION_KEY] = admin.id
    session.permanent = True


def logout_admin() -> None:
    session.pop(ADMIN_SESSION_KEY, None)
    g._admin_cache = None


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_admin() is None:
            flash("Требуется вход в систему.", "warning")
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper
