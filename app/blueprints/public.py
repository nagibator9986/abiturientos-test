"""Публичная часть: регистрация абитуриента, прохождение теста, результат."""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..extensions import db
from ..models import Attempt, Question, Variant
from ..security import client_ip
from ..services import (
    AttemptFinishedError,
    close_stale_attempts,
    create_attempt,
    get_setting_bool,
    get_setting_int,
    grade_attempt,
    save_answer,
    seconds_left,
    validate_applicant,
)

bp = Blueprint("public", __name__)

SESSION_ATTEMPTS_KEY = "attempt_ids"
GRACE_SECONDS = 30  # допуск на задержку сети при автосдаче по таймеру


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _remember_attempt(attempt: Attempt) -> None:
    ids = list(session.get(SESSION_ATTEMPTS_KEY, []))
    if attempt.public_id not in ids:
        ids.append(attempt.public_id)
    session[SESSION_ATTEMPTS_KEY] = ids[-20:]
    session.permanent = True


def _owns(attempt: Attempt) -> bool:
    return attempt.public_id in set(session.get(SESSION_ATTEMPTS_KEY, []))


def _get_attempt_or_404(public_id: str) -> Attempt:
    attempt = Attempt.query.filter_by(public_id=public_id).one_or_none()
    if attempt is None:
        abort(404)
    return attempt


def _require_owned(public_id: str) -> Attempt:
    attempt = _get_attempt_or_404(public_id)
    if not _owns(attempt):
        abort(403)
    return attempt


def _finalize_if_expired(attempt: Attempt) -> bool:
    """Завершает попытку, если время вышло. Возвращает True, если завершили."""
    if attempt.status != Attempt.STATUS_IN_PROGRESS:
        return False
    if attempt.deadline_at and seconds_left(attempt) <= 0:
        grade_attempt(attempt, expired=True)
        return True
    return False


# --------------------------------------------------------------------------- #
# Страницы
# --------------------------------------------------------------------------- #


@bp.route("/", methods=["GET"])
def index():
    close_stale_attempts()
    variants = Variant.query.filter_by(is_active=True).order_by(Variant.code).all()
    return render_template(
        "public/start.html",
        variants=variants,
        form={},
        errors={},
        duration=get_setting_int("duration_minutes", 60),
    )


@bp.route("/start", methods=["POST"])
def start():
    variants = Variant.query.filter_by(is_active=True).order_by(Variant.code).all()
    if not variants:
        flash("Нет доступных вариантов теста. Обратитесь в приёмную комиссию.", "error")
        return redirect(url_for("public.index"))

    form = request.form.to_dict()
    applicant, errors = validate_applicant(form)

    variant = None
    raw_variant = (form.get("variant_id") or "").strip()
    if raw_variant:
        variant = next((v for v in variants if str(v.id) == raw_variant), None)
        if variant is None:
            errors["variant_id"] = "Выберите вариант теста из списка."
    else:
        errors["variant_id"] = "Выберите вариант теста."

    if not errors and variant is not None and not get_setting_bool("allow_multiple_attempts", True):
        # учитываем и незавершённые попытки: иначе запрет обходится тремя вкладками
        already = (
            Attempt.query.filter(
                Attempt.full_name_norm == applicant["full_name"].lower(),
                Attempt.variant_id == variant.id,
            )
            .order_by(Attempt.started_at.desc())
            .first()
        )
        if already is not None:
            if already.status == Attempt.STATUS_IN_PROGRESS and seconds_left(already) > 0 and _owns(already):
                # своя незаконченная работа — возвращаем к ней, а не создаём новую
                return redirect(url_for("public.test", public_id=already.public_id))
            errors["full_name"] = "Этот абитуриент уже проходил данный вариант теста."

    if errors:
        return (
            render_template(
                "public/start.html",
                variants=variants,
                form=form,
                errors=errors,
                duration=get_setting_int("duration_minutes", 60),
            ),
            400,
        )

    attempt = create_attempt(
        variant,
        applicant,
        ip=client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
    )
    _remember_attempt(attempt)
    return redirect(url_for("public.test", public_id=attempt.public_id))


@bp.route("/test/<public_id>", methods=["GET"])
def test(public_id: str):
    attempt = _require_owned(public_id)
    _finalize_if_expired(attempt)

    if attempt.is_finished:
        return redirect(url_for("public.result", public_id=attempt.public_id))

    questions = attempt.variant.questions
    saved = {a.question_id: a.letter for a in attempt.answers if a.option_id is not None}
    sections: list[dict] = []
    for question in questions:
        if not sections or sections[-1]["name"] != question.section:
            sections.append({"name": question.section, "questions": []})
        sections[-1]["questions"].append(question)

    response = make_response(
        render_template(
            "public/test.html",
            attempt=attempt,
            variant=attempt.variant,
            questions=questions,
            sections=sections,
            saved=saved,
            seconds_left=seconds_left(attempt),
            total=len(questions),
        )
    )
    # страницу теста нельзя доставать из кэша «назад» после сдачи работы
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@bp.route("/test/<public_id>/submit", methods=["POST"])
def submit(public_id: str):
    attempt = _require_owned(public_id)

    if attempt.is_finished:
        return redirect(url_for("public.result", public_id=attempt.public_id))

    # Страховка: забираем ответы из самой формы. Если автосохранение по сети
    # не сработало (сбой связи, отключённый JS), ответы всё равно не потеряются.
    stored = {a.question_id: a.letter for a in attempt.answers if a.option_id is not None}
    for question in attempt.variant.questions:
        chosen = (request.form.get(f"q{question.id}") or "").strip().upper()[:2]
        if chosen and chosen != stored.get(question.id):
            try:
                save_answer(attempt, question, chosen)
            except (ValueError, AttemptFinishedError):
                continue

    expired = bool(attempt.deadline_at) and seconds_left(attempt) <= 0
    grade_attempt(attempt, expired=expired)
    flash("Тест завершён. Результат сохранён.", "success")
    return redirect(url_for("public.result", public_id=attempt.public_id))


@bp.route("/result/<public_id>", methods=["GET"])
def result(public_id: str):
    attempt = _require_owned(public_id)
    _finalize_if_expired(attempt)

    if not attempt.is_finished:
        return redirect(url_for("public.test", public_id=attempt.public_id))

    answers = {a.question_id: a for a in attempt.answers}
    section_stats: dict[str, dict[str, int]] = {}
    for question in attempt.variant.questions:
        stats = section_stats.setdefault(question.section or "—", {"total": 0, "correct": 0})
        stats["total"] += 1
        answer = answers.get(question.id)
        if answer is not None and answer.is_correct:
            stats["correct"] += 1
    for stats in section_stats.values():
        stats["percent"] = round(stats["correct"] * 100 / stats["total"], 1) if stats["total"] else 0.0

    return render_template(
        "public/result.html",
        attempt=attempt,
        answers=answers,
        section_stats=section_stats,
        show_score=get_setting_bool("show_score_to_student", True),
        show_review=get_setting_bool("show_review_to_student", False),
        pass_percent=get_setting_int("pass_percent", 60),
    )


# --------------------------------------------------------------------------- #
# API прохождения теста (автосохранение)
# --------------------------------------------------------------------------- #


@bp.route("/api/attempt/<public_id>/answer", methods=["POST"])
def api_save_answer(public_id: str):
    attempt = _get_attempt_or_404(public_id)
    if not _owns(attempt):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if _finalize_if_expired(attempt) or attempt.is_finished:
        return jsonify({"ok": False, "error": "finished", "message": "Время вышло, тест завершён."}), 409

    payload = request.get_json(silent=True) or {}
    try:
        question_id = int(payload.get("question_id", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_question"}), 400

    letter = (payload.get("letter") or "").strip().upper()[:2]

    question = db.session.get(Question, question_id)
    if question is None or question.variant_id != attempt.variant_id:
        return jsonify({"ok": False, "error": "bad_question"}), 400

    try:
        save_answer(attempt, question, letter or None)
    except ValueError:
        return jsonify({"ok": False, "error": "bad_option"}), 400
    except AttemptFinishedError:
        return jsonify({"ok": False, "error": "finished", "message": "Работа уже завершена."}), 409

    answered = attempt.answered_count
    return jsonify(
        {
            "ok": True,
            "question_id": question_id,
            "letter": letter,
            "answered": answered,
            "total": attempt.total_questions,
            "seconds_left": seconds_left(attempt),
        }
    )


@bp.route("/api/attempt/<public_id>/state", methods=["GET"])
def api_state(public_id: str):
    attempt = _get_attempt_or_404(public_id)
    if not _owns(attempt):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify(
        {
            "ok": True,
            "status": attempt.status,
            "answered": attempt.answered_count,
            "total": attempt.total_questions,
            "seconds_left": seconds_left(attempt),
            "answers": {str(a.question_id): a.letter for a in attempt.answers if a.option_id is not None},
        }
    )


@bp.route("/api/attempt/<public_id>/expire", methods=["POST"])
def api_expire(public_id: str):
    """Автозавершение по таймеру со стороны браузера."""
    attempt = _get_attempt_or_404(public_id)
    if not _owns(attempt):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if attempt.is_finished:
        return jsonify({"ok": True, "redirect": url_for("public.result", public_id=public_id)})
    if attempt.deadline_at and seconds_left(attempt) > GRACE_SECONDS:
        return jsonify({"ok": False, "error": "too_early", "seconds_left": seconds_left(attempt)}), 400
    grade_attempt(attempt, expired=True)
    return jsonify({"ok": True, "redirect": url_for("public.result", public_id=public_id)})
