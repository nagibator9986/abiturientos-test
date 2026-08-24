"""Бизнес-логика: настройки, загрузка банка вопросов, проверка и статистика."""
from __future__ import annotations

import json
import re
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import Answer, Attempt, AdminUser, Option, Question, Setting, Variant, utcnow

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS: dict[str, str] = {
    "institution_name": "Caspian College",
    "test_title": "Вступительный тест по английскому языку",
    "duration_minutes": "60",
    "pass_percent": "60",
    "show_score_to_student": "1",
    "show_review_to_student": "0",
    "allow_multiple_attempts": "1",
    "shuffle_questions": "0",
    "timezone": "Asia/Almaty",
}

# Часовые пояса, доступные в настройках админ-панели
TIMEZONES = ["Asia/Almaty", "Asia/Aqtau", "Asia/Oral", "Asia/Bishkek", "Europe/Moscow", "UTC"]


def get_setting(key: str, default: str | None = None) -> str:
    row = db.session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key, "")


def get_setting_int(key: str, default: int) -> int:
    try:
        return int(str(get_setting(key, str(default))).strip())
    except (TypeError, ValueError):
        return default


def get_setting_bool(key: str, default: bool = False) -> bool:
    return str(get_setting(key, "1" if default else "0")).strip() in {"1", "true", "on", "yes"}


def set_setting(key: str, value: Any) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.value = "" if value is None else str(value)


def all_settings() -> dict[str, str]:
    data = dict(DEFAULT_SETTINGS)
    for row in Setting.query.all():
        data[row.key] = row.value or ""
    return data


# --------------------------------------------------------------------------- #
# Часовой пояс (в БД всё хранится в UTC, показывается — в местном времени)
# --------------------------------------------------------------------------- #


def get_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(get_setting("timezone", "Asia/Almaty"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def to_local(value: datetime | None) -> datetime | None:
    """Наивный UTC из БД → местное время для показа."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(get_timezone())


def local_today() -> date_cls:
    return datetime.now(get_timezone()).date()


def utc_bounds_for_local_date(day: date_cls, end_of_day: bool = False) -> datetime:
    """Границы местных суток в наивном UTC — для фильтров по датам."""
    tz = get_timezone()
    moment = datetime.combine(day, time_cls.min, tzinfo=tz)
    if end_of_day:
        moment = moment + timedelta(days=1) - timedelta(microseconds=1)
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_bounds(offset_days: int = 0) -> tuple[datetime, datetime, date_cls]:
    """(начало, конец) местных суток в UTC и сама местная дата."""
    day = local_today() - timedelta(days=offset_days)
    return utc_bounds_for_local_date(day), utc_bounds_for_local_date(day + timedelta(days=1)), day


# --------------------------------------------------------------------------- #
# Оценка и уровень
# --------------------------------------------------------------------------- #

GRADE_SCALE: list[tuple[float, str, str, str]] = [
    # (нижняя граница %, оценка, уровень CEFR, цвет-тег)
    (90.0, "Отлично", "C1", "emerald"),
    (75.0, "Хорошо", "B2", "sky"),
    (60.0, "Удовлетворительно", "B1", "amber"),
    (40.0, "Ниже среднего", "A2", "orange"),
    (0.0, "Неудовлетворительно", "A1", "rose"),
]


def grade_for(percent: float) -> tuple[str, str, str]:
    """Возвращает (оценка, уровень CEFR, цвет) по проценту правильных ответов."""
    for threshold, grade, level, color in GRADE_SCALE:
        if percent >= threshold:
            return grade, level, color
    return "Неудовлетворительно", "A1", "rose"


def grade_color(percent: float) -> str:
    return grade_for(percent)[2]


# --------------------------------------------------------------------------- #
# Валидация данных абитуриента
# --------------------------------------------------------------------------- #

# первая буква — любая буква Юникода (кириллица, латиница с диакритикой, казахские);
# далее допускаются буквы, пробел, дефис и апостроф
NAME_RE = re.compile(r"^[^\W\d_][^\W\d_\-'’` ]*(?:[\-'’` ][^\W\d_][^\W\d_\-'’` ]*)*$", re.UNICODE)
PHONE_RE = re.compile(r"^[\d\+\-\(\)\s]{5,25}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def normalize_name(raw: str) -> str:
    """Убирает лишние пробелы и приводит каждое слово к виду Иванов."""
    parts = [p for p in re.split(r"\s+", (raw or "").strip()) if p]
    fixed = []
    for part in parts:
        chunks = []
        for chunk in part.split("-"):
            if not chunk:
                chunks.append(chunk)
            elif chunk.islower() or chunk.isupper():
                # «иванов» / «ИВАНОВ» → «Иванов»; «МакЛауд» оставляем как есть
                chunks.append(chunk.capitalize())
            else:
                chunks.append(chunk[0].upper() + chunk[1:])
        fixed.append("-".join(chunks))
    return " ".join(fixed)


def validate_applicant(form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Проверяет данные абитуриента. Возвращает (очищенные данные, ошибки)."""
    errors: dict[str, str] = {}

    full_name = normalize_name(form.get("full_name", ""))
    if len(full_name) < 5:
        errors["full_name"] = "Введите ФИО полностью (минимум 5 символов)."
    elif len(full_name) > 160:
        errors["full_name"] = "ФИО слишком длинное (максимум 160 символов)."
    elif len(full_name.split()) < 2:
        errors["full_name"] = "Укажите как минимум фамилию и имя."
    elif not NAME_RE.match(full_name):
        errors["full_name"] = "ФИО может содержать только буквы, пробел, дефис и апостроф."

    phone = (form.get("phone") or "").strip()
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "Телефон указан в неверном формате."

    email = (form.get("email") or "").strip()
    if email and not EMAIL_RE.match(email):
        errors["email"] = "E-mail указан в неверном формате."

    school = (form.get("school") or "").strip()[:200]

    return (
        {
            "full_name": full_name,
            "phone": phone[:40],
            "email": email[:160],
            "school": school,
        },
        errors,
    )


# --------------------------------------------------------------------------- #
# Загрузка банка вопросов из JSON
# --------------------------------------------------------------------------- #


def load_variant_files(data_dir: Path) -> list[dict]:
    files = sorted(Path(data_dir).glob("variant_*.json"))
    payloads = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            payloads.append(json.load(fh))
    return payloads


def sync_variants(data_dir: Path, verbose: bool = False, force: bool = False) -> dict[str, int]:
    """Идемпотентно синхронизирует варианты/вопросы/ответы из JSON в БД.

    Существующие вопросы обновляются на месте (по позиции), поэтому ссылки из
    таблицы answers и история попыток сохраняются даже при правке банка.

    Защита истории: если формулировка вопроса изменилась, а по этому вопросу уже
    есть сохранённые ответы, вопрос НЕ перезаписывается (иначе ответы абитуриентов
    молча «переедут» на другой вопрос). Такие вопросы попадают в stats["skipped"];
    перезапись выполняется явно — sync_variants(..., force=True) или flask sync --force.
    """
    stats = {"variants": 0, "questions": 0, "options": 0, "updated": 0, "skipped": 0}

    for payload in load_variant_files(data_dir):
        code = payload["code"]
        variant = Variant.query.filter_by(code=code).one_or_none()
        if variant is None:
            variant = Variant(code=code)
            db.session.add(variant)
            stats["variants"] += 1
        variant.title = payload.get("title", code)
        variant.description = payload.get("description", "")
        variant.duration_minutes = int(payload.get("duration_minutes", 60))
        if variant.is_active is None:
            variant.is_active = True
        db.session.flush()

        seen_positions = set()
        for item in payload["questions"]:
            position = int(item["n"])
            seen_positions.add(position)
            question = Question.query.filter_by(variant_id=variant.id, position=position).one_or_none()
            if question is None:
                question = Question(variant_id=variant.id, position=position)
                db.session.add(question)
                stats["questions"] += 1
            else:
                prompt_changed = (question.prompt or "").strip() != str(item["prompt"]).strip()
                if prompt_changed and not force:
                    answered = Answer.query.filter_by(question_id=question.id).count()
                    if answered:
                        stats["skipped"] += 1
                        if verbose:
                            print(
                                f"  ! {code} №{position}: формулировка изменилась, но по вопросу "
                                f"есть {answered} ответ(ов) — пропущено (используйте --force)"
                            )
                        continue
                stats["updated"] += 1
            question.section = item.get("section", "")
            question.instruction = item.get("instruction") or None
            question.passage = item.get("passage") or None
            question.prompt = item["prompt"]
            db.session.flush()

            correct = str(item["answer"]).strip().upper()
            letters = list(item["options"].keys())
            for letter, text in item["options"].items():
                option = Option.query.filter_by(question_id=question.id, letter=letter).one_or_none()
                if option is None:
                    option = Option(question_id=question.id, letter=letter)
                    db.session.add(option)
                    stats["options"] += 1
                option.text = text
                option.is_correct = letter == correct
            # лишние варианты ответа удаляем только если на них никто не отвечал
            for option in list(question.options):
                if option.letter not in letters:
                    if Answer.query.filter_by(option_id=option.id).count():
                        stats["skipped"] += 1
                        if verbose:
                            print(f"  ! {code} №{position}: вариант {option.letter} выбран абитуриентами — не удалён")
                        continue
                    db.session.delete(option)

        # вопросы, исчезнувшие из файла, удаляем только при отсутствии ответов:
        # каскад иначе стёр бы ответы уже сданных работ
        for question in Question.query.filter_by(variant_id=variant.id).all():
            if question.position in seen_positions:
                continue
            if Answer.query.filter_by(question_id=question.id).count():
                stats["skipped"] += 1
                if verbose:
                    print(f"  ! {code} №{question.position}: по вопросу есть ответы — не удалён")
                continue
            db.session.delete(question)

    db.session.commit()
    if verbose:
        print(f"Синхронизация банка вопросов: {stats}")
    return stats


def ensure_schema() -> list[str]:
    """Добавляет недостающие колонки в существующую БД (мини-миграция SQLite)."""
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    expected = {
        "attempts": {
            "search_text": "VARCHAR(700) NOT NULL DEFAULT ''",
            "school": "VARCHAR(200) DEFAULT ''",
            "last_activity_at": "DATETIME",
        },
        "questions": {
            "instruction": "TEXT",
        },
    }

    for table, columns in expected.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                added.append(f"{table}.{name}")

    if added:
        db.session.commit()
        if any(name.startswith("attempts.") for name in added):
            for attempt in Attempt.query.all():
                attempt.refresh_search_text()
            db.session.commit()
    return added


def ensure_admin(username: str, password: str) -> AdminUser:
    admin = AdminUser.query.filter_by(username=username).one_or_none()
    if admin is None:
        admin = AdminUser(username=username)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
    return admin


def ensure_settings() -> None:
    existing = {row.key for row in Setting.query.all()}
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            db.session.add(Setting(key=key, value=value))
            changed = True
    if changed:
        db.session.commit()


# --------------------------------------------------------------------------- #
# Попытки: создание, сохранение ответов, проверка
# --------------------------------------------------------------------------- #


def create_attempt(variant: Variant, applicant: dict[str, str], ip: str, user_agent: str) -> Attempt:
    duration = get_setting_int("duration_minutes", variant.duration_minutes or 60)
    started = utcnow()
    attempt = Attempt(
        variant_id=variant.id,
        full_name=applicant["full_name"],
        full_name_norm=applicant["full_name"].lower(),
        phone=applicant.get("phone", ""),
        email=applicant.get("email", ""),
        school=applicant.get("school", ""),
        status=Attempt.STATUS_IN_PROGRESS,
        started_at=started,
        deadline_at=started + timedelta(minutes=duration) if duration > 0 else None,
        total_questions=variant.question_count,
        unanswered_count=variant.question_count,
        ip_address=(ip or "")[:64],
        user_agent=(user_agent or "")[:300],
    )
    db.session.add(attempt)
    db.session.flush()          # нужен public_id для поисковой строки
    attempt.refresh_search_text()
    db.session.commit()
    return attempt


class AttemptFinishedError(RuntimeError):
    """Попытка уже завершена — ответы больше не принимаются."""


def save_answer(attempt: Attempt, question: Question, letter: str | None) -> Answer:
    """Сохраняет (или очищает) ответ. Правильность фиксируется сразу же.

    Устойчиво к гонке двух параллельных запросов по одному вопросу (две вкладки,
    двойной клик): при конфликте уникального индекса запись обновляется повторно.
    """
    option = None
    if letter:
        option = next((o for o in question.options if o.letter == letter), None)
        if option is None:
            raise ValueError(f"Вариант ответа '{letter}' отсутствует у вопроса {question.position}")

    def apply(answer: Answer) -> None:
        if option is None:
            answer.option_id = None
            answer.letter = ""
            answer.is_correct = False
        else:
            answer.option_id = option.id
            answer.letter = option.letter
            answer.is_correct = bool(option.is_correct)
        answer.updated_at = utcnow()

    # Атомарная проверка статуса: условный UPDATE берёт блокировку записи и
    # отсекает ответ, пришедший одновременно с подсчётом результата.
    locked = db.session.execute(
        update(Attempt)
        .where(Attempt.id == attempt.id, Attempt.status == Attempt.STATUS_IN_PROGRESS)
        .values(last_activity_at=utcnow())
    )
    if locked.rowcount == 0:
        db.session.rollback()
        raise AttemptFinishedError("Работа уже завершена")

    answer = Answer.query.filter_by(attempt_id=attempt.id, question_id=question.id).one_or_none()
    if answer is not None:
        apply(answer)
        db.session.commit()
        return answer

    answer = Answer(attempt_id=attempt.id, question_id=question.id)
    apply(answer)
    db.session.add(answer)
    try:
        db.session.commit()
    except IntegrityError:
        # параллельный запрос успел создать строку — обновляем её
        db.session.rollback()
        answer = Answer.query.filter_by(attempt_id=attempt.id, question_id=question.id).one()
        apply(answer)
        db.session.commit()
    return answer


def grade_attempt(attempt: Attempt, expired: bool = False, keep_timing: bool = False) -> Attempt:
    """Подсчитывает результат попытки и переводит её в финальный статус.

    keep_timing=True сохраняет уже зафиксированные время завершения и длительность
    (используется при пересчёте ранее сданных работ по обновлённому ключу).
    """
    questions = attempt.variant.questions
    answers = {a.question_id: a for a in attempt.answers}

    correct = wrong = unanswered = 0
    for question in questions:
        answer = answers.get(question.id)
        if answer is None or answer.option_id is None:
            if answer is not None:
                answer.is_correct = False   # вариант ответа удалён из банка — признак устарел
            unanswered += 1
            continue
        # пересчитываем по актуальному ключу — на случай правок банка вопросов
        answer.is_correct = bool(answer.option and answer.option.is_correct)
        if answer.is_correct:
            correct += 1
        else:
            wrong += 1

    total = len(questions)
    percent = round(correct * 100.0 / total, 2) if total else 0.0
    grade, level, _color = grade_for(percent)

    attempt.total_questions = total
    attempt.correct_count = correct
    attempt.wrong_count = wrong
    attempt.unanswered_count = unanswered
    attempt.percent = percent
    attempt.grade = grade
    attempt.level = level

    if not (keep_timing and attempt.finished_at):
        # для просроченной работы моментом сдачи считается окончание отведённого
        # времени, а не момент, когда систему об этом «уведомили»
        if expired and attempt.deadline_at:
            finished = min(utcnow(), max(attempt.deadline_at, attempt.started_at))
        else:
            finished = utcnow()
        attempt.finished_at = finished
        attempt.duration_seconds = max(0, int((finished - attempt.started_at).total_seconds()))

    attempt.status = Attempt.STATUS_EXPIRED if expired else Attempt.STATUS_SUBMITTED

    db.session.commit()
    return attempt


def regrade_all() -> int:
    """Пересчитывает все завершённые попытки (после изменения ключа ответов)."""
    count = 0
    for attempt in Attempt.query.filter(Attempt.status != Attempt.STATUS_IN_PROGRESS).all():
        grade_attempt(attempt, expired=attempt.status == Attempt.STATUS_EXPIRED, keep_timing=True)
        count += 1
    return count


_last_sweep_at = 0.0
SWEEP_INTERVAL_SECONDS = 60
SWEEP_BATCH_LIMIT = 200


def close_stale_attempts(grace_seconds: int = 120, force: bool = False) -> int:
    """Автозавершение попыток, у которых истёк срок (например, окно закрыли).

    Вызывается со страниц, поэтому выполняется не чаще раза в минуту и обрабатывает
    ограниченную порцию записей — публичный запрос не должен превращаться
    в длинную транзакцию.
    """
    global _last_sweep_at
    import time as _time

    if not force and _time.monotonic() - _last_sweep_at < SWEEP_INTERVAL_SECONDS:
        return 0
    _last_sweep_at = _time.monotonic()

    now = utcnow()
    stale = (
        Attempt.query.filter(Attempt.status == Attempt.STATUS_IN_PROGRESS)
        .filter(Attempt.deadline_at.isnot(None))
        .filter(Attempt.deadline_at < now - timedelta(seconds=grace_seconds))
        .limit(SWEEP_BATCH_LIMIT)
        .all()
    )
    for attempt in stale:
        grade_attempt(attempt, expired=True)
    return len(stale)


def seconds_left(attempt: Attempt) -> int:
    if not attempt.deadline_at:
        return 0
    return max(0, int((attempt.deadline_at - utcnow()).total_seconds()))


# --------------------------------------------------------------------------- #
# Статистика для админ-панели
# --------------------------------------------------------------------------- #


def dashboard_stats() -> dict[str, Any]:
    finished = Attempt.query.filter(Attempt.status != Attempt.STATUS_IN_PROGRESS)
    total = Attempt.query.count()
    finished_count = finished.count()
    in_progress = total - finished_count

    avg_percent = db.session.query(func.avg(Attempt.percent)).filter(
        Attempt.status != Attempt.STATUS_IN_PROGRESS
    ).scalar()
    avg_percent = round(float(avg_percent), 1) if avg_percent is not None else 0.0

    pass_percent = get_setting_int("pass_percent", 60)
    passed = finished.filter(Attempt.percent >= pass_percent).count()

    today_start, _today_end, _today_date = local_day_bounds(0)
    week_start, _e, _d = local_day_bounds(6)
    today = Attempt.query.filter(Attempt.started_at >= today_start).count()
    week = Attempt.query.filter(Attempt.started_at >= week_start).count()

    best = (
        finished.order_by(Attempt.percent.desc(), Attempt.duration_seconds.asc())
        .limit(5)
        .all()
    )
    recent = Attempt.query.order_by(Attempt.started_at.desc()).limit(8).all()

    by_variant = []
    for variant in Variant.query.order_by(Variant.code).all():
        qs = finished.filter(Attempt.variant_id == variant.id)
        count = qs.count()
        avg = db.session.query(func.avg(Attempt.percent)).filter(
            Attempt.variant_id == variant.id, Attempt.status != Attempt.STATUS_IN_PROGRESS
        ).scalar()
        by_variant.append(
            {
                "variant": variant,
                "count": count,
                "avg": round(float(avg), 1) if avg is not None else 0.0,
            }
        )

    buckets = [("0–39 %", 0, 40), ("40–59 %", 40, 60), ("60–74 %", 60, 75), ("75–89 %", 75, 90), ("90–100 %", 90, 101)]
    distribution = []
    for label, low, high in buckets:
        count = finished.filter(Attempt.percent >= low, Attempt.percent < high).count()
        distribution.append({"label": label, "count": count})
    max_bucket = max((d["count"] for d in distribution), default=0) or 1
    for item in distribution:
        item["ratio"] = round(item["count"] * 100 / max_bucket)

    # активность за 14 дней
    activity = []
    for offset in range(13, -1, -1):
        day_start, day_end, day_date = local_day_bounds(offset)
        count = Attempt.query.filter(Attempt.started_at >= day_start, Attempt.started_at < day_end).count()
        activity.append({"date": day_date, "count": count})
    max_day = max((d["count"] for d in activity), default=0) or 1
    for item in activity:
        item["ratio"] = round(item["count"] * 100 / max_day)

    return {
        "total": total,
        "finished": finished_count,
        "in_progress": in_progress,
        "avg_percent": avg_percent,
        "passed": passed,
        "pass_rate": round(passed * 100 / finished_count, 1) if finished_count else 0.0,
        "pass_percent": pass_percent,
        "today": today,
        "week": week,
        "best": best,
        "recent": recent,
        "by_variant": by_variant,
        "distribution": distribution,
        "activity": activity,
    }


def hardest_questions(variant_id: int | None = None, limit: int = 10) -> list[dict]:
    """Вопросы с наибольшей долей ошибок — полезно приёмной комиссии."""
    query = (
        db.session.query(
            Answer.question_id,
            func.count(Answer.id).label("answered"),
            func.sum(func.cast(Answer.is_correct, db.Integer)).label("correct"),
        )
        .join(Attempt, Attempt.id == Answer.attempt_id)
        .filter(Attempt.status != Attempt.STATUS_IN_PROGRESS)
        .filter(Answer.option_id.isnot(None))
        .group_by(Answer.question_id)
    )
    if variant_id:
        query = query.filter(Attempt.variant_id == variant_id)

    rows = []
    for question_id, answered, correct in query.all():
        question = db.session.get(Question, question_id)
        if question is None or answered < 1:
            continue
        correct = int(correct or 0)
        rows.append(
            {
                "question": question,
                "answered": answered,
                "correct": correct,
                "accuracy": round(correct * 100 / answered, 1),
            }
        )
    rows.sort(key=lambda r: (r["accuracy"], -r["answered"]))
    return rows[:limit]


def format_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "—"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин" if sec == 0 else f"{minutes} мин {sec:02d} с"
    return f"{sec} с"


def csv_safe(value: str) -> str:
    """Защита от формульной инъекции при открытии CSV в Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def iter_export_rows(attempts: Iterable[Attempt]) -> Iterable[list[str]]:
    yield [
        "ID", "ФИО", "Телефон", "E-mail", "Школа/колледж", "Вариант", "Статус",
        "Начало", "Завершение", "Длительность", "Правильных", "Ошибок",
        "Без ответа", "Всего", "Процент", "Оценка", "Уровень",
    ]
    status_map = {
        Attempt.STATUS_SUBMITTED: "Завершён",
        Attempt.STATUS_EXPIRED: "Время истекло",
        Attempt.STATUS_IN_PROGRESS: "В процессе",
    }
    for attempt in attempts:
        started = to_local(attempt.started_at)
        finished = to_local(attempt.finished_at)
        done = attempt.status != Attempt.STATUS_IN_PROGRESS
        yield [
            attempt.public_id,
            csv_safe(attempt.full_name),
            csv_safe(attempt.phone or ""),
            csv_safe(attempt.email or ""),
            csv_safe(attempt.school or ""),
            attempt.variant.code if attempt.variant else "",
            status_map.get(attempt.status, attempt.status),
            started.strftime("%d.%m.%Y %H:%M") if started else "",
            finished.strftime("%d.%m.%Y %H:%M") if finished else "",
            format_duration(attempt.duration_seconds) if done else "",
            str(attempt.correct_count) if done else "",
            str(attempt.wrong_count) if done else "",
            str(attempt.unanswered_count) if done else "",
            str(attempt.total_questions),
            f"{attempt.percent:.1f}" if done else "",
            attempt.grade or "" if done else "",
            attempt.level or "" if done else "",
        ]


# --------------------------------------------------------------------------- #
# Демонстрационные данные (для показа админ-панели)
# --------------------------------------------------------------------------- #

DEMO_SURNAMES = [
    "Ахметова", "Бекжанов", "Сериков", "Ким", "Нурланова", "Абдрахманов", "Ли",
    "Жумабаева", "Оспанов", "Тлеубеков", "Искакова", "Мухамедов", "Петрова",
    "Смагулов", "Ерболатова", "Каримов", "Сагындыкова", "Досжанов",
]
DEMO_NAMES = [
    "Дана", "Ернар", "Алмас", "Виктория", "Айгуль", "Тимур", "Асель", "Данияр",
    "Мадина", "Руслан", "Камила", "Нурлан", "Аружан", "Ислам", "Диана", "Санжар",
]
DEMO_PATRONYMICS = [
    "Ерлановна", "Маратулы", "Сериковна", "Аскарович", "Нурлановна", "Бекетулы",
    "Талгатовна", "Ержанович", "", "",
]
DEMO_SCHOOLS = [
    "Гимназия №5 г. Алматы", "СШ №25 г. Актау", "Колледж КазГЮУ", "НИШ г. Астана",
    "Лицей №8 г. Атырау", "СШ им. Абая, г. Шымкент", "Колледж Caspian",
]


def generate_demo_attempts(count: int = 25, days: int = 14) -> int:
    """Создаёт демонстрационные попытки со случайными ответами.

    Нужно только для показа возможностей админ-панели; на реальные данные
    не влияет — записи создаются как обычные попытки.
    """
    import random
    from datetime import timedelta

    variants = Variant.query.filter_by(is_active=True).all()
    if not variants:
        return 0

    created = 0
    for _ in range(count):
        variant = random.choice(variants)
        surname = random.choice(DEMO_SURNAMES)
        name = random.choice(DEMO_NAMES)
        patronymic = random.choice(DEMO_PATRONYMICS)
        full_name = " ".join(p for p in (surname, name, patronymic) if p)

        started = utcnow() - timedelta(
            days=random.randint(0, days - 1), hours=random.randint(0, 10), minutes=random.randint(0, 59)
        )
        duration = random.randint(12, 55)
        attempt = Attempt(
            variant_id=variant.id,
            full_name=full_name,
            phone=f"+7 7{random.randint(0, 9)}{random.randint(0, 9)} {random.randint(100, 999)} {random.randint(10, 99)} {random.randint(10, 99)}",
            email=f"{name.lower()}.{surname.lower()[:6]}@mail.kz",
            school=random.choice(DEMO_SCHOOLS),
            status=Attempt.STATUS_SUBMITTED,
            started_at=started,
            deadline_at=started + timedelta(minutes=get_setting_int("duration_minutes", 60)),
            total_questions=variant.question_count,
        )
        db.session.add(attempt)
        db.session.flush()
        attempt.refresh_search_text()

        # моделируем уровень подготовки абитуриента
        skill = random.random() ** 0.8
        for question in variant.questions:
            roll = random.random()
            if roll > 0.94:  # пропустил вопрос
                continue
            correct = roll < skill
            option = question.correct_option if correct else next(
                (o for o in question.options if not o.is_correct), None
            )
            if option is None:
                continue
            db.session.add(
                Answer(
                    attempt_id=attempt.id,
                    question_id=question.id,
                    option_id=option.id,
                    letter=option.letter,
                    is_correct=bool(option.is_correct),
                    updated_at=started + timedelta(minutes=random.randint(1, duration)),
                )
            )
        db.session.flush()
        grade_attempt(attempt)
        attempt.finished_at = started + timedelta(minutes=duration)
        attempt.duration_seconds = duration * 60
        created += 1

    db.session.commit()
    return created
