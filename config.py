"""Конфигурация приложения Caspian College Entrance Testing."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DATA_DIR = BASE_DIR / "data"


class BaseConfig:
    """Общие настройки."""

    # Значения по умолчанию нет намеренно: ключ берётся из переменной окружения
    # SECRET_KEY, иначе приложение генерирует случайный и хранит его в instance/secret_key.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'testing.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        # SQLite + многопоточный dev-сервер: ждём блокировку вместо мгновенной ошибки.
        "connect_args": {"timeout": 30, "check_same_thread": False},
        "pool_pre_ping": True,
    }

    JSON_AS_ASCII = False
    JSONIFY_PRETTYPRINT_REGULAR = False
    TEMPLATES_AUTO_RELOAD = False

    # Сессии
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 часов

    MAX_CONTENT_LENGTH = 1 * 1024 * 1024  # 1 МБ — защита от больших POST

    # Данные тестов
    DATA_DIR = DATA_DIR

    # Учётная запись администратора по умолчанию (создаётся при первом запуске)
    DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123321")

    # Защита формы входа в админку
    LOGIN_MAX_ATTEMPTS = 8
    LOGIN_LOCKOUT_SECONDS = 300

    # Доверять заголовку X-Forwarded-For (включать только за обратным прокси)
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    TEMPLATES_AUTO_RELOAD = True


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = os.environ.get("FORCE_HTTPS", "0") == "1"


CONFIGS = {"development": DevelopmentConfig, "production": ProductionConfig}


def get_config(name: str | None = None):
    name = (name or os.environ.get("FLASK_ENV") or "development").lower()
    return CONFIGS.get(name, DevelopmentConfig)
