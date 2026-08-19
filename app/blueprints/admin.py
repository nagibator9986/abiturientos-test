"""Админ-панель: вход, дашборд, поиск по результатам, экспорт, настройки."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import AdminUser, Attempt, Variant, utcnow
from ..security import (
    admin_required,
    client_ip,
    current_admin,
    login_admin,
    login_locked_seconds,
    logout_admin,
    register_failed_login,
    reset_login_attempts,
)
from ..services import (
    DEFAULT_SETTINGS,
    TIMEZONES,
    all_settings,
    close_stale_attempts,
    dashboard_stats,
    grade_for,
    hardest_questions,
    iter_export_rows,
    regrade_all,
    set_setting,
    utc_bounds_for_local_date,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")

SORT_COLUMNS = {
    "date": Attempt.started_at,
    "name": Attempt.full_name_norm,
    "percent": Attempt.percent,
    "correct": Attempt.correct_count,
    "duration": Attempt.duration_seconds,
    "variant": Attempt.variant_id,
    "status": Attempt.status,
}
PER_PAGE_CHOICES = (20, 50, 100, 200)


# --------------------------------------------------------------------------- #
# Аутентификация
# --------------------------------------------------------------------------- #


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_admin() is not None:
        return redirect(url_for("admin.dashboard"))

    ip = client_ip()
    error = None

    if request.method == "POST":
        locked = login_locked_seconds(ip)
        if locked:
            error = f"Слишком много попыток входа. Повторите через {locked // 60 + 1} мин."
        else:
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            admin = AdminUser.query.filter_by(username=username).one_or_none()
            if admin is not None and admin.check_password(password):
                reset_login_attempts(ip)
                admin.last_login_at = utcnow()
                db.session.commit()
                login_admin(admin)
                target = request.args.get("next") or request.form.get("next") or ""
                if not target.startswith("/admin"):
                    target = url_for("admin.dashboard")
                return redirect(target)
            register_failed_login(ip)
            error = "Неверный логин или пароль."

    return render_template("admin/login.html", error=error, next=request.args.get("next", "")), (
        401 if error else 200
    )


@bp.route("/logout", methods=["POST"])
@admin_required
def logout():
    logout_admin()
    flash("Вы вышли из системы.", "success")
    return redirect(url_for("admin.login"))


# --------------------------------------------------------------------------- #
# Дашборд
# --------------------------------------------------------------------------- #


@bp.route("/", methods=["GET"])
@admin_required
def dashboard():
    close_stale_attempts()
    stats = dashboard_stats()
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        hardest=hardest_questions(limit=8),
        variants=Variant.query.order_by(Variant.code).all(),
    )


# --------------------------------------------------------------------------- #
# Результаты: поиск, фильтры, экспорт
# --------------------------------------------------------------------------- #


def _parse_date(raw: str | None, end_of_day: bool = False):
    """Дата из формы трактуется как местная и переводится в UTC для запроса."""
    if not raw:
        return None
    try:
        value = datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return utc_bounds_for_local_date(value, end_of_day=end_of_day)


def _filters_from_request() -> dict:
    return {
        "q": (request.args.get("q") or "").strip(),
        "variant": (request.args.get("variant") or "").strip(),
        "status": (request.args.get("status") or "").strip(),
        "date_from": (request.args.get("date_from") or "").strip(),
        "date_to": (request.args.get("date_to") or "").strip(),
        "min_percent": (request.args.get("min_percent") or "").strip(),
        "max_percent": (request.args.get("max_percent") or "").strip(),
        "sort": (request.args.get("sort") or "date").strip(),
        "order": (request.args.get("order") or "desc").strip(),
    }


def _build_conditions(filters: dict) -> list:
    """Собирает список SQL-условий по параметрам поиска."""
    conditions = []

    term = filters["q"]
    if term:
        needle = f"%{term.lower()}%"
        # search_text уже приведён к нижнему регистру в Python — это единственный
        # надёжный способ регистронезависимого поиска по кириллице в SQLite.
        conditions.append(
            or_(Attempt.search_text.like(needle), Attempt.full_name_norm.like(needle))
        )

    if filters["variant"].isdigit():
        conditions.append(Attempt.variant_id == int(filters["variant"]))

    if filters["status"] in {Attempt.STATUS_SUBMITTED, Attempt.STATUS_EXPIRED, Attempt.STATUS_IN_PROGRESS}:
        conditions.append(Attempt.status == filters["status"])
    elif filters["status"] == "finished":
        conditions.append(Attempt.status != Attempt.STATUS_IN_PROGRESS)

    date_from = _parse_date(filters["date_from"])
    if date_from:
        conditions.append(Attempt.started_at >= date_from)
    date_to = _parse_date(filters["date_to"], end_of_day=True)
    if date_to:
        conditions.append(Attempt.started_at <= date_to)

    for key, is_min in (("min_percent", True), ("max_percent", False)):
        raw = filters[key]
        if not raw:
            continue
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        conditions.append(Attempt.percent >= value if is_min else Attempt.percent <= value)

    return conditions


def _order_clause(filters: dict):
    column = SORT_COLUMNS.get(filters["sort"], Attempt.started_at)
    return column.desc() if filters["order"] == "desc" else column.asc()


def _select_attempts(filters: dict):
    return select(Attempt).where(*_build_conditions(filters)).order_by(_order_clause(filters))


@bp.route("/attempts", methods=["GET"])
@admin_required
def attempts():
    close_stale_attempts()
    filters = _filters_from_request()
    conditions = _build_conditions(filters)
    statement = select(Attempt).where(*conditions).order_by(_order_clause(filters))

    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20
    if per_page not in PER_PAGE_CHOICES:
        per_page = 20

    total_rows = db.session.scalar(select(func.count()).select_from(Attempt).where(*conditions)) or 0
    total_pages = max(1, -(-total_rows // per_page))
    page = min(max(1, request.args.get("page", 1, type=int) or 1), total_pages)
    pagination = db.paginate(statement, page=page, per_page=per_page, error_out=False)

    finished = [*conditions, Attempt.status != Attempt.STATUS_IN_PROGRESS]
    summary = {
        "count": total_rows,
        "finished": db.session.scalar(select(func.count()).select_from(Attempt).where(*finished)) or 0,
        "avg": round(float(db.session.scalar(select(func.avg(Attempt.percent)).where(*finished)) or 0), 1),
        "best": round(float(db.session.scalar(select(func.max(Attempt.percent)).where(*finished)) or 0), 1),
    }

    # только известные параметры попадают в ссылки — url_for нельзя кормить
    # произвольными ключами из строки запроса (например, «_external»)
    link_args = {key: value for key, value in filters.items() if value}
    link_args["per_page"] = per_page

    return render_template(
        "admin/attempts.html",
        pagination=pagination,
        items=pagination.items,
        filters=filters,
        link_args=link_args,
        per_page=per_page,
        per_page_choices=PER_PAGE_CHOICES,
        variants=Variant.query.order_by(Variant.code).all(),
        summary=summary,
    )


@bp.route("/attempts/export.csv", methods=["GET"])
@admin_required
def export_csv():
    filters = _filters_from_request()
    rows = db.session.scalars(_select_attempts(filters)).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    for row in iter_export_rows(rows):
        writer.writerow(row)

    payload = "﻿" + buffer.getvalue()  # BOM — чтобы Excel открыл UTF-8 корректно
    filename = f"caspian-results-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/attempts/<public_id>", methods=["GET"])
@admin_required
def attempt_detail(public_id: str):
    attempt = Attempt.query.filter_by(public_id=public_id).one_or_none()
    if attempt is None:
        abort(404)

    answers = {a.question_id: a for a in attempt.answers}
    rows = []
    section_stats: dict[str, dict] = {}
    for question in attempt.variant.questions:
        answer = answers.get(question.id)
        rows.append(
            {
                "question": question,
                "answer": answer,
                "given": answer.letter if answer and answer.option_id else "",
                "correct": question.correct_letter,
                "is_correct": bool(answer and answer.is_correct),
            }
        )
        stats = section_stats.setdefault(question.section or "—", {"total": 0, "correct": 0})
        stats["total"] += 1
        if answer is not None and answer.is_correct:
            stats["correct"] += 1
    for stats in section_stats.values():
        stats["percent"] = round(stats["correct"] * 100 / stats["total"], 1) if stats["total"] else 0.0

    grade, level, color = grade_for(attempt.percent)
    return render_template(
        "admin/attempt_detail.html",
        attempt=attempt,
        rows=rows,
        section_stats=section_stats,
        grade=grade,
        level=level,
        color=color,
    )


@bp.route("/attempts/<public_id>/delete", methods=["POST"])
@admin_required
def attempt_delete(public_id: str):
    attempt = Attempt.query.filter_by(public_id=public_id).one_or_none()
    if attempt is None:
        abort(404)
    name = attempt.full_name
    db.session.delete(attempt)
    db.session.commit()
    flash(f"Попытка «{name}» удалена.", "success")
    return redirect(url_for("admin.attempts"))


# --------------------------------------------------------------------------- #
# Банк вопросов
# --------------------------------------------------------------------------- #


@bp.route("/questions", methods=["GET"])
@admin_required
def questions():
    variants = Variant.query.order_by(Variant.code).all()
    selected_id = request.args.get("variant", type=int)
    selected = next((v for v in variants if v.id == selected_id), variants[0] if variants else None)
    return render_template(
        "admin/questions.html",
        variants=variants,
        selected=selected,
        hardest=hardest_questions(variant_id=selected.id if selected else None, limit=100),
    )


@bp.route("/variants/<int:variant_id>/toggle", methods=["POST"])
@admin_required
def variant_toggle(variant_id: int):
    variant = db.session.get(Variant, variant_id)
    if variant is None:
        abort(404)
    variant.is_active = not variant.is_active
    db.session.commit()
    flash(
        f"Вариант {variant.code} {'включён' if variant.is_active else 'отключён'}.",
        "success",
    )
    return redirect(request.referrer or url_for("admin.questions"))


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

EDITABLE_SETTINGS = {
    "institution_name": str,
    "test_title": str,
    "timezone": str,
    "duration_minutes": int,
    "pass_percent": int,
    "show_score_to_student": bool,
    "show_review_to_student": bool,
    "allow_multiple_attempts": bool,
}


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        errors: list[str] = []
        pending: dict[str, Any] = {}
        # маркер полной формы: без него неотмеченные галочки не считаются выключёнными
        full_form = request.form.get("_settings_form") == "1"

        # 1) проверяем всё и только потом записываем — частично применённых настроек не бывает
        for key, kind in EDITABLE_SETTINGS.items():
            raw = request.form.get(key)

            if kind is bool:
                if full_form:
                    pending[key] = "1" if raw else "0"
                continue

            if raw is None:
                continue                      # поле не пришло — оставляем прежнее значение
            raw = raw.strip()

            if kind is int:
                try:
                    value = int(raw)
                except ValueError:
                    errors.append(f"Поле «{key}» должно быть числом.")
                    continue
                if key == "duration_minutes" and not (1 <= value <= 600):
                    errors.append("Длительность теста должна быть от 1 до 600 минут.")
                    continue
                if key == "pass_percent" and not (0 <= value <= 100):
                    errors.append("Проходной балл должен быть от 0 до 100 %.")
                    continue
                pending[key] = value
            elif key == "timezone":
                if raw in TIMEZONES:
                    pending[key] = raw
                else:
                    errors.append("Выбран неизвестный часовой пояс.")
            elif raw:
                pending[key] = raw[:200]
            # пустая строка не затирает уже сохранённое значение

        if not errors:
            for key, value in pending.items():
                set_setting(key, value)

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            db.session.commit()
            flash("Настройки сохранены.", "success")
        return redirect(url_for("admin.settings_page"))

    return render_template(
        "admin/settings.html",
        values=all_settings(),
        defaults=DEFAULT_SETTINGS,
        timezones=TIMEZONES,
        admin=current_admin(),
    )


@bp.route("/settings/password", methods=["POST"])
@admin_required
def change_password():
    admin = current_admin()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    repeat = request.form.get("repeat_password") or ""

    if not admin.check_password(current):
        flash("Текущий пароль указан неверно.", "error")
    elif len(new) < 8:
        flash("Новый пароль должен содержать минимум 8 символов.", "error")
    elif new != repeat:
        flash("Пароли не совпадают.", "error")
    else:
        admin.set_password(new)
        db.session.commit()
        flash("Пароль обновлён.", "success")
    return redirect(url_for("admin.settings_page"))


@bp.route("/regrade", methods=["POST"])
@admin_required
def regrade():
    count = regrade_all()
    flash(f"Пересчитано попыток: {count}.", "success")
    return redirect(request.referrer or url_for("admin.dashboard"))
