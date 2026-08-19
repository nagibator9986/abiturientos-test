"""Точка входа: python3 run.py — запуск сервера платформы тестирования."""
from __future__ import annotations

import os
import socket

from app import create_app

app = create_app()

DEFAULT_PORT = 8080


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    """Возвращает свободный порт: на macOS 5000 занят AirPlay, 8000 — частый конфликт."""
    for candidate in range(preferred, preferred + 10):
        if port_is_free(candidate):
            return candidate
    return preferred


if __name__ == "__main__":
    requested = int(os.environ.get("PORT", DEFAULT_PORT))
    port = pick_port(requested)
    host = os.environ.get("HOST", "0.0.0.0")
    # отладчик Werkzeug включается только явно: FLASK_DEBUG=1 python3 run.py
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print("=" * 64)
    print("  Caspian College — платформа вступительного тестирования")
    if port != requested:
        print(f"  Порт {requested} занят другой программой — используем {port}")
    print(f"  Тестирование:  http://127.0.0.1:{port}/")
    print(f"  Админ-панель:  http://127.0.0.1:{port}/admin  (admin / admin123321)")
    print("  Остановить: Ctrl+C")
    print("=" * 64)

    app.run(host=host, port=port, debug=debug, use_reloader=debug)
