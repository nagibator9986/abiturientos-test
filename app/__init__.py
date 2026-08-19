"""Фабрика приложения Caspian College Entrance Testing."""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

import click
from flask import Flask, render_template, request

from config import get_config
from .extensions import db
from .security import csrf_protect, current_admin, generate_csrf_token

__all__ = ["create_app", "db"]


def create_app(config_name: str | None = None, overrides: dict | None = None) -> Flask:
    """Создаёт приложение. overrides позволяет подменить настройки (используется в тестах)."""
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(get_config(config_name))
    app.config.update(overrides or {})
    if "SECRET_KEY" not in (overrides or {}):
        app.config["SECRET_KEY"] = _resolve_secret_key(app)

    Path(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")).parent.mkdir(
        parents=True, exist_ok=True
    )

    db.init_app(app)

    from .blueprints.public import bp as public_bp
    from .blueprints.admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    app.before_request(csrf_protect)

    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)

    with app.app_context():
        bootstrap(app)

    return app


def _resolve_secret_key(app: Flask) -> str:
    """SECRET_KEY: из окружения, иначе — постоянный ключ в instance/secret_key.

    Случайный ключ, созданный при первом запуске, лучше значения по умолчанию:
    его нельзя подобрать, зная исходный код, а сессии переживают перезапуск.
    """
    from_env = os.environ.get("SECRET_KEY")
    if from_env:
        return from_env

    key_path = Path(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")).parent / "secret_key"
    try:
        if key_path.exists():
            value = key_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        key_path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        key_path.write_text(value, encoding="utf-8")
        key_path.chmod(0o600)
        return value
    except OSError:
        return app.config.get("SECRET_KEY", "caspian-college-entrance-testing")


def bootstrap(app: Flask) -> None:
    """Создаёт таблицы, администратора, настройки и загружает банк вопросов.

    Выполняется под файловой блокировкой: при запуске несколькими воркерами
    (gunicorn -w 4) инициализацию делает только один процесс, остальные ждут.
    """
    import fcntl

    lock_path = Path(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")).parent / "bootstrap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except OSError:  # pragma: no cover - файловая система без flock
            pass
        _bootstrap_locked(app)


def _bootstrap_locked(app: Flask) -> None:
    from .models import AdminUser
    from .services import ensure_admin, ensure_schema, ensure_settings, sync_variants

    db.create_all()
    ensure_schema()
    ensure_settings()
    ensure_admin(app.config["DEFAULT_ADMIN_USERNAME"], app.config["DEFAULT_ADMIN_PASSWORD"])
    if os.environ.get("SKIP_SYNC") != "1":
        sync_variants(app.config["DATA_DIR"])

    if not app.debug and not os.environ.get("ADMIN_PASSWORD"):
        admin = AdminUser.query.filter_by(username=app.config["DEFAULT_ADMIN_USERNAME"]).first()
        if admin and admin.check_password(app.config["DEFAULT_ADMIN_PASSWORD"]):
            app.logger.warning(
                "Используется пароль администратора по умолчанию — смените его в разделе "
                "«Настройки» или командой flask set-admin-password."
            )


def _register_template_helpers(app: Flask) -> None:
    from .services import all_settings, format_duration, grade_color, to_local

    @app.context_processor
    def inject_globals():
        settings = all_settings()
        return {
            "csrf_token": generate_csrf_token,
            "settings": settings,
            "current_admin": current_admin(),
            "current_year": datetime.now().year,
        }

    @app.template_filter("dt")
    def _dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        """Время из БД (UTC) показывается в часовом поясе из настроек."""
        local = to_local(value)
        return local.strftime(fmt) if local else "—"

    @app.template_filter("duration")
    def _duration(value: int | None) -> str:
        return format_duration(value)

    @app.template_filter("gcolor")
    def _gcolor(value: float) -> str:
        return grade_color(value or 0.0)

    @app.template_filter("nbsp_num")
    def _nbsp_num(value) -> str:
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            return str(value)


def _register_error_handlers(app: Flask) -> None:
    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.is_json

    @app.errorhandler(400)
    def bad_request(error):
        if wants_json():
            return {"ok": False, "error": "bad_request", "message": str(getattr(error, "description", ""))}, 400
        return render_template("errors/error.html", code=400, title="Некорректный запрос",
                               message=getattr(error, "description", "Проверьте данные и повторите попытку.")), 400

    @app.errorhandler(403)
    def forbidden(error):
        if wants_json():
            return {"ok": False, "error": "forbidden"}, 403
        return render_template("errors/error.html", code=403, title="Доступ запрещён",
                               message="У вас нет прав для просмотра этой страницы."), 403

    @app.errorhandler(404)
    def not_found(error):
        if wants_json():
            return {"ok": False, "error": "not_found"}, 404
        return render_template("errors/error.html", code=404, title="Страница не найдена",
                               message="Проверьте адрес страницы — возможно, она была удалена."), 404

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover
        db.session.rollback()
        if wants_json():
            return {"ok": False, "error": "server_error"}, 500
        return render_template("errors/error.html", code=500, title="Внутренняя ошибка",
                               message="Что-то пошло не так. Попробуйте повторить действие."), 500


def _register_cli(app: Flask) -> None:
    from .models import Attempt, AdminUser, Variant
    from .services import ensure_admin, ensure_settings, regrade_all, sync_variants

    @app.cli.command("init-db")
    def init_db_cmd():
        """Создать таблицы и загрузить банк вопросов."""
        bootstrap(app)
        click.echo("База данных готова.")

    @app.cli.command("sync")
    @click.option("--force", is_flag=True, help="Перезаписать даже вопросы, по которым уже есть ответы")
    def sync_cmd(force: bool):
        """Синхронизировать банк вопросов из data/*.json."""
        stats = sync_variants(app.config["DATA_DIR"], verbose=True, force=force)
        click.echo(f"Готово: {stats}")
        if stats.get("skipped"):
            click.echo("Часть вопросов пропущена для сохранности истории — повторите с --force.")

    @app.cli.command("regrade")
    def regrade_cmd():
        """Пересчитать результаты всех завершённых попыток."""
        count = regrade_all()
        click.echo(f"Пересчитано попыток: {count}")

    @app.cli.command("set-admin-password")
    @click.argument("password")
    @click.option("--username", default=None, help="Имя администратора")
    def set_admin_password_cmd(password: str, username: str | None):
        """Задать новый пароль администратора."""
        username = username or app.config["DEFAULT_ADMIN_USERNAME"]
        admin = ensure_admin(username, password)
        admin.set_password(password)
        db.session.commit()
        click.echo(f"Пароль для «{username}» обновлён.")

    @app.cli.command("demo-data")
    @click.argument("count", type=int, default=25)
    def demo_data_cmd(count: int):
        """Создать демонстрационные попытки (для показа админ-панели)."""
        from .services import generate_demo_attempts

        created = generate_demo_attempts(count)
        click.echo(f"Создано демонстрационных работ: {created}")

    @app.cli.command("reset-attempts")
    @click.confirmation_option(prompt="Удалить ВСЕ попытки и ответы?")
    def reset_attempts_cmd():
        """Полностью очистить результаты тестирования."""
        count = Attempt.query.count()
        for attempt in Attempt.query.all():
            db.session.delete(attempt)
        db.session.commit()
        click.echo(f"Удалено попыток: {count}")

    @app.cli.command("stats")
    def stats_cmd():
        """Краткая сводка по базе."""
        click.echo(f"Вариантов: {Variant.query.count()}")
        click.echo(f"Попыток:   {Attempt.query.count()}")
        click.echo(f"Админов:   {AdminUser.query.count()}")
