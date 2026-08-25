"""Сквозные тесты платформы: регистрация, прохождение, подсчёт, админка."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Attempt, Variant  # noqa: E402


@pytest.fixture()
def app():
    """Изолированное приложение на временной БД — рабочая база не затрагивается."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app(
        "development",
        overrides={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
            "SECRET_KEY": "test-secret-key",
        },
    )
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF-токен не найден на странице"
    return match.group(1)


def start_attempt(client, name="Иванов Иван Иванович", variant_code="1-в", **extra):
    page = client.get("/")
    token = csrf_from(page.get_data(as_text=True))
    variant = Variant.query.filter_by(code=variant_code).one()
    payload = {
        "csrf_token": token,
        "full_name": name,
        "variant_id": variant.id,
        "phone": "+7 701 000 00 00",
    }
    payload.update(extra)
    response = client.post("/start", data=payload, follow_redirects=False)
    assert response.status_code == 302
    public_id = response.headers["Location"].rstrip("/").split("/")[-1]
    return public_id, token, variant


# --------------------------------------------------------------------------- #


def test_bank_loaded(app):
    with app.app_context():
        variants = Variant.query.order_by(Variant.code).all()
        assert [v.code for v in variants] == ["1-в", "2-в"]
        assert [v.question_count for v in variants] == [50, 50]
        for variant in variants:
            for question in variant.questions:
                assert len(question.options) >= 3
                assert sum(1 for o in question.options if o.is_correct) == 1


def test_instructions_are_loaded(app):
    """Описания заданий («Choose the appropriate answer» и т. п.) попадают в банк."""
    with app.app_context():
        from app.models import Question

        expected = {
            ("1-в", 41): "Read the sentence below, then choose the best answer to the question.",
            ("1-в", 46): "Choose the appropriate answer",
            ("1-в", 31): "Read some texts and find the right answers",
            ("1-в", 49): "Read two sentences below and choose the best way of combining them.",
            ("2-в", 33): "Read the sentence below, then choose the best answer to the question.",
            ("2-в", 38): "Choose the appropriate answer",
            ("2-в", 41): "Read some texts and find the right answers",
            ("2-в", 30): "Read two sentences below and choose the best way of combining them.",
        }
        for (code, position), instruction in expected.items():
            variant = Variant.query.filter_by(code=code).one()
            question = Question.query.filter_by(variant_id=variant.id, position=position).one()
            assert question.instruction == instruction, f"{code} №{position}"


def test_corrections_from_methodologist(app):
    """Правки приёмной комиссии от 24.08.2026 применены к банку вопросов."""
    with app.app_context():
        from app.models import Question

        v1 = Variant.query.filter_by(code="1-в").one()
        v2 = Variant.query.filter_by(code="2-в").one()

        def q(variant, position):
            return Question.query.filter_by(variant_id=variant.id, position=position).one()

        assert q(v1, 18).correct_letter == "D"                       # «So do I»
        assert q(v2, 3).correct_letter == "C"                        # «Isn’t going to»
        assert q(v2, 6).prompt.endswith("some food.")                # добавлено «food»
        assert "Have you ever visited" in q(v2, 8).prompt            # добавлено «ever»
        # из варианта 2-в убран только спорный вопрос «You ___ better see a doctor»;
        # вопрос «she ___ from her job yesterday» оставлен в тесте
        assert v2.question_count == 50
        prompts = " ".join(question.prompt for question in v2.questions)
        assert "better see a doctor" not in prompts
        assert q(v2, 29).prompt.endswith("from her job yesterday.")
        assert q(v2, 29).correct_letter == "B"                       # «Resigned»
        # нумерация осталась непрерывной 1..50
        assert [question.position for question in v2.questions] == list(range(1, 51))


def test_start_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Регистрация абитуриента" in response.get_data(as_text=True)


def test_validation_rejects_bad_name(client, app):
    with app.app_context():
        page = client.get("/")
        token = csrf_from(page.get_data(as_text=True))
        variant = Variant.query.filter_by(code="1-в").one()
        response = client.post("/start", data={"csrf_token": token, "full_name": "Ы", "variant_id": variant.id})
        assert response.status_code == 400
        assert "ФИО" in response.get_data(as_text=True)


def test_csrf_required(client):
    response = client.post("/start", data={"full_name": "Иванов Иван", "variant_id": 1})
    assert response.status_code == 400


def test_full_pass_scores_100(client, app):
    """Отвечаем строго по ключу — результат должен быть 100 %."""
    with app.app_context():
        public_id, token, variant = start_attempt(client)
        questions = Variant.query.filter_by(code="1-в").one().questions
        for question in questions:
            response = client.post(
                f"/api/attempt/{public_id}/answer",
                json={"csrf_token": token, "question_id": question.id, "letter": question.correct_letter},
                headers={"X-CSRF-Token": token},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["ok"] is True

        submit = client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        assert submit.status_code == 302

        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.status == "submitted"
        assert attempt.correct_count == len(questions)
        assert attempt.percent == 100.0
        assert attempt.grade == "Отлично"
        assert attempt.unanswered_count == 0


def test_partial_and_unanswered(client, app):
    with app.app_context():
        public_id, token, _ = start_attempt(client, name="Петров Пётр")
        questions = Variant.query.filter_by(code="1-в").one().questions
        for index, question in enumerate(questions[:10]):
            wrong = next(o.letter for o in question.options if not o.is_correct)
            letter = question.correct_letter if index < 6 else wrong
            client.post(
                f"/api/attempt/{public_id}/answer",
                json={"question_id": question.id, "letter": letter},
                headers={"X-CSRF-Token": token},
            )
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.correct_count == 6
        assert attempt.wrong_count == 4
        assert attempt.unanswered_count == len(questions) - 10
        assert attempt.percent == round(6 * 100 / len(questions), 2)


def test_answer_can_be_changed_and_cleared(client, app):
    with app.app_context():
        public_id, token, _ = start_attempt(client)
        question = Variant.query.filter_by(code="1-в").one().questions[0]
        wrong = next(o.letter for o in question.options if not o.is_correct)

        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": wrong},
                    headers={"X-CSRF-Token": token})
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": question.correct_letter},
                    headers={"X-CSRF-Token": token})
        state = client.get(f"/api/attempt/{public_id}/state").get_json()
        assert state["answers"][str(question.id)] == question.correct_letter
        assert state["answered"] == 1

        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": ""},
                    headers={"X-CSRF-Token": token})
        state = client.get(f"/api/attempt/{public_id}/state").get_json()
        assert state["answered"] == 0


def test_cross_variant_question_rejected(client, app):
    with app.app_context():
        public_id, token, _ = start_attempt(client, variant_code="1-в")
        foreign = Variant.query.filter_by(code="2-в").one().questions[0]
        response = client.post(
            f"/api/attempt/{public_id}/answer",
            json={"question_id": foreign.id, "letter": "A"},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 400


def test_foreign_attempt_forbidden(app):
    with app.app_context():
        owner = app.test_client()
        public_id, _, _ = start_attempt(owner)
        stranger = app.test_client()
        assert stranger.get(f"/test/{public_id}").status_code == 403
        assert stranger.get(f"/api/attempt/{public_id}/state").status_code == 403


def test_admin_requires_login(client):
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def admin_login(client, username="admin", password="admin123321"):
    page = client.get("/admin/login")
    token = csrf_from(page.get_data(as_text=True))
    return client.post("/admin/login", data={"csrf_token": token, "username": username, "password": password},
                       follow_redirects=True)


def test_admin_login_and_search(client, app):
    with app.app_context():
        public_id, token, _ = start_attempt(client, name="Сидорова Айгуль Маратовна")
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})

        assert admin_login(client, password="wrong").status_code == 401
        assert admin_login(client).status_code == 200

        dashboard = client.get("/admin/")
        assert dashboard.status_code == 200
        assert "Дашборд" in dashboard.get_data(as_text=True)

        # поиск в нижнем регистре по кириллице
        found = client.get("/admin/attempts?q=айгуль").get_data(as_text=True)
        assert "Сидорова Айгуль Маратовна" in found

        missing = client.get("/admin/attempts?q=несуществующий").get_data(as_text=True)
        assert "Ничего не найдено" in missing

        detail = client.get(f"/admin/attempts/{public_id}")
        assert detail.status_code == 200
        assert "Лист ответов" in detail.get_data(as_text=True)

        export = client.get("/admin/attempts/export.csv")
        assert export.status_code == 200
        assert "Сидорова Айгуль Маратовна" in export.get_data(as_text=True)


def test_admin_settings_and_questions(client, app):
    with app.app_context():
        admin_login(client)
        page = client.get("/admin/settings")
        assert page.status_code == 200
        token = csrf_from(page.get_data(as_text=True))

        response = client.post(
            "/admin/settings",
            data={"csrf_token": token, "_settings_form": "1", "institution_name": "Caspian College",
                  "test_title": "Тест", "timezone": "Asia/Almaty", "duration_minutes": "45",
                  "pass_percent": "70", "show_score_to_student": "on"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        from app.services import get_setting, get_setting_bool, get_setting_int

        assert get_setting_int("duration_minutes", 0) == 45
        assert get_setting_int("pass_percent", 0) == 70
        assert get_setting_bool("show_score_to_student") is True
        # снятая галочка выключает настройку
        assert get_setting_bool("show_review_to_student") is False

        # неполный POST не должен затирать уже сохранённые значения
        page = client.get("/admin/settings")
        client.post("/admin/settings",
                    data={"csrf_token": csrf_from(page.get_data(as_text=True)), "institution_name": ""},
                    follow_redirects=True)
        assert get_setting("institution_name") == "Caspian College"
        assert get_setting_int("duration_minutes", 0) == 45

        # некорректная длительность отклоняется без изменения настроек
        page = client.get("/admin/settings")
        client.post("/admin/settings",
                    data={"csrf_token": csrf_from(page.get_data(as_text=True)),
                          "_settings_form": "1", "duration_minutes": "0"},
                    follow_redirects=True)
        assert get_setting_int("duration_minutes", 0) == 45

        questions_page = client.get("/admin/questions")
        assert questions_page.status_code == 200
        assert "Банк вопросов" in questions_page.get_data(as_text=True)


def test_sync_is_idempotent(app):
    with app.app_context():
        from app.services import sync_variants

        before = {v.code: v.question_count for v in Variant.query.all()}
        sync_variants(app.config["DATA_DIR"])
        sync_variants(app.config["DATA_DIR"])
        after = {v.code: v.question_count for v in Variant.query.all()}
        assert before == after
        assert Variant.query.count() == 2


def test_name_is_normalized(client, app):
    """ФИО из лишних пробелов и нижнего регистра приводится к единому виду."""
    with app.app_context():
        public_id, _, _ = start_attempt(client, name="  тестов   тимур  алиевич ")
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.full_name == "Тестов Тимур Алиевич"


def test_admin_search_is_case_insensitive_for_cyrillic(client, app):
    """Поиск по кириллице работает в любом регистре и по всем полям карточки."""
    with app.app_context():
        public_id, token, _ = start_attempt(
            client,
            name="Сидорова Айгуль Маратовна",
            phone="+7 701 555 33 22",
            email="a.sidorova@mail.kz",
            school="СШ №25 г. Актау",
        )
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        admin_login(client)

        for query in ["сидорова", "СИДОРОВА", "айгуль", "Актау", "актау", "АКТАУ",
                      "555 33", "a.sidorova", "СШ №25", public_id[:8]]:
            body = client.get("/admin/attempts", query_string={"q": query}).get_data(as_text=True)
            assert "Сидорова Айгуль Маратовна" in body, f"не найдено по запросу: {query}"

        empty = client.get("/admin/attempts", query_string={"q": "Алматы"}).get_data(as_text=True)
        assert "Ничего не найдено" in empty


def test_admin_filters_and_sorting(client, app):
    with app.app_context():
        good_id, token, _ = start_attempt(client, name="Отличников Алмас")
        questions = Variant.query.filter_by(code="1-в").one().questions
        for question in questions:
            client.post(f"/api/attempt/{good_id}/answer",
                        json={"question_id": question.id, "letter": question.correct_letter},
                        headers={"X-CSRF-Token": token})
        client.post(f"/test/{good_id}/submit", data={"csrf_token": token})

        weak_id, token2, _ = start_attempt(client, name="Слабов Ержан")
        client.post(f"/test/{weak_id}/submit", data={"csrf_token": token2})

        admin_login(client)
        high = client.get("/admin/attempts", query_string={"min_percent": "90"}).get_data(as_text=True)
        assert "Отличников Алмас" in high and "Слабов Ержан" not in high

        low = client.get("/admin/attempts", query_string={"max_percent": "10"}).get_data(as_text=True)
        assert "Слабов Ержан" in low and "Отличников Алмас" not in low

        variant2 = Variant.query.filter_by(code="2-в").one()
        other = client.get("/admin/attempts", query_string={"variant": variant2.id}).get_data(as_text=True)
        assert "Ничего не найдено" in other

        ordered = client.get("/admin/attempts", query_string={"sort": "percent", "order": "asc"})
        assert ordered.status_code == 200
        page = ordered.get_data(as_text=True)
        assert page.index("Слабов Ержан") < page.index("Отличников Алмас")


def test_deleting_attempt_removes_answers(client, app):
    with app.app_context():
        from app.models import Answer

        public_id, token, _ = start_attempt(client, name="Удаляев Дамир")
        question = Variant.query.filter_by(code="1-в").one().questions[0]
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": question.correct_letter},
                    headers={"X-CSRF-Token": token})
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        admin_login(client)

        attempt_id = Attempt.query.filter_by(public_id=public_id).one().id
        assert Answer.query.filter_by(attempt_id=attempt_id).count() == 1

        page = client.get(f"/admin/attempts/{public_id}").get_data(as_text=True)
        response = client.post(f"/admin/attempts/{public_id}/delete",
                               data={"csrf_token": csrf_from(page)}, follow_redirects=True)
        assert response.status_code == 200
        assert Attempt.query.filter_by(public_id=public_id).count() == 0
        assert Answer.query.filter_by(attempt_id=attempt_id).count() == 0


def test_expired_attempt_is_graded_and_locked(client, app):
    """Просроченная попытка автоматически закрывается, ответы больше не принимаются."""
    with app.app_context():
        from datetime import timedelta

        from app.models import utcnow

        public_id, token, _ = start_attempt(client, name="Опоздалов Нурлан")
        question = Variant.query.filter_by(code="1-в").one().questions[0]
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": question.correct_letter},
                    headers={"X-CSRF-Token": token})

        attempt = Attempt.query.filter_by(public_id=public_id).one()
        attempt.deadline_at = utcnow() - timedelta(minutes=1)
        db.session.commit()

        late = client.post(f"/api/attempt/{public_id}/answer",
                           json={"question_id": question.id, "letter": question.correct_letter},
                           headers={"X-CSRF-Token": token})
        assert late.status_code == 409

        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.status == "expired"
        assert attempt.correct_count == 1  # сохранённый ответ засчитан

        page = client.get(f"/test/{public_id}", follow_redirects=False)
        assert page.status_code == 302 and "/result/" in page.headers["Location"]


def test_regrade_after_key_change(client, app):
    """Изменение ключа + пересчёт корректно обновляет уже сданные работы."""
    with app.app_context():
        from app.services import regrade_all

        public_id, token, _ = start_attempt(client, name="Ключевой Тест")
        question = Variant.query.filter_by(code="1-в").one().questions[0]
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": question.id, "letter": question.correct_letter},
                    headers={"X-CSRF-Token": token})
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        assert Attempt.query.filter_by(public_id=public_id).one().correct_count == 1

        # меняем ключ: правильным становится другой вариант ответа
        original_letter = question.correct_letter
        new_letter = next(o.letter for o in question.options if o.letter != original_letter)
        for option in question.options:
            option.is_correct = option.letter == new_letter
        db.session.commit()

        regrade_all()
        assert Attempt.query.filter_by(public_id=public_id).one().correct_count == 0


def test_submit_saves_answers_from_form_without_js(client, app):
    """Если автосохранение не сработало, ответы приходят с формой и учитываются."""
    with app.app_context():
        public_id, token, _ = start_attempt(client, name="Безджаваскриптов Артём")
        questions = Variant.query.filter_by(code="1-в").one().questions
        payload = {"csrf_token": token}
        for question in questions[:15]:
            payload[f"q{question.id}"] = question.correct_letter

        client.post(f"/test/{public_id}/submit", data=payload)
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.correct_count == 15
        assert attempt.status == "submitted"


def test_timezone_affects_display_and_filters(client, app):
    """Время показывается в местном поясе, фильтр по дате трактуется так же."""
    with app.app_context():
        from datetime import datetime, timezone as dt_timezone

        from app.services import set_setting, to_local, utc_bounds_for_local_date

        set_setting("timezone", "Asia/Almaty")
        db.session.commit()

        moment = datetime(2026, 5, 10, 20, 30)          # 20:30 UTC
        local = to_local(moment)
        assert (local.hour, local.day) == (1, 11)        # 01:30 следующего дня в Актау/Алматы

        start = utc_bounds_for_local_date(datetime(2026, 5, 11).date())
        assert start == datetime(2026, 5, 10, 19, 0)     # местная полночь = 19:00 UTC

        set_setting("timezone", "UTC")
        db.session.commit()
        assert to_local(moment).hour == 20


def test_regrade_keeps_original_timing(client, app):
    """Пересчёт не подменяет время сдачи и длительность работы."""
    with app.app_context():
        from app.services import regrade_all

        public_id, token, _ = start_attempt(client, name="Времянов Олег")
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        finished_before, duration_before = attempt.finished_at, attempt.duration_seconds

        regrade_all()
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.finished_at == finished_before
        assert attempt.duration_seconds == duration_before


def test_csv_export_escapes_formulas(client, app):
    """ФИО, начинающееся с «=», не должно исполняться формулой в Excel."""
    with app.app_context():
        public_id, token, _ = start_attempt(client, name="Иванов Иван", school="=SUM(A1:A9)")
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        admin_login(client)
        body = client.get("/admin/attempts/export.csv").get_data(as_text=True)
        assert "'=SUM(A1:A9)" in body
        assert ";=SUM" not in body


def test_expired_attempt_duration_matches_deadline(client, app):
    """У просроченной работы длительность равна отведённому времени, а не паузе до обнаружения."""
    with app.app_context():
        from datetime import timedelta

        from app.models import utcnow
        from app.services import close_stale_attempts

        public_id, _, _ = start_attempt(client, name="Забытов Ильяс")
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        attempt.started_at = utcnow() - timedelta(hours=5)
        attempt.deadline_at = attempt.started_at + timedelta(minutes=60)
        db.session.commit()

        assert close_stale_attempts(force=True) == 1
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.status == "expired"
        assert 3500 <= attempt.duration_seconds <= 3700   # ≈ 60 минут, а не 5 часов


def test_answer_rejected_after_grading(client, app):
    """Ответ, пришедший после подсчёта результата, не принимается и не портит статистику."""
    with app.app_context():
        from app.services import AttemptFinishedError, save_answer

        public_id, token, _ = start_attempt(client, name="Поздняков Артур")
        questions = Variant.query.filter_by(code="1-в").one().questions
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": questions[0].id, "letter": questions[0].correct_letter},
                    headers={"X-CSRF-Token": token})
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})

        attempt = Attempt.query.filter_by(public_id=public_id).one()
        with pytest.raises(AttemptFinishedError):
            save_answer(attempt, questions[1], questions[1].correct_letter)

        late = client.post(f"/api/attempt/{public_id}/answer",
                           json={"question_id": questions[1].id, "letter": questions[1].correct_letter},
                           headers={"X-CSRF-Token": token})
        assert late.status_code == 409

        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.correct_count == 1
        assert attempt.answered_count == 1


def test_sync_protects_history(app):
    """Правка формулировки и удаление вопроса не трогают уже сохранённые ответы."""
    with app.app_context():
        import json
        import shutil
        import tempfile
        from pathlib import Path

        from app.models import Answer, Question
        from app.services import sync_variants

        client = app.test_client()
        public_id, token, _ = start_attempt(client)
        questions = Variant.query.filter_by(code="1-в").one().questions
        client.post(f"/api/attempt/{public_id}/answer",
                    json={"question_id": questions[0].id, "letter": questions[0].correct_letter},
                    headers={"X-CSRF-Token": token})
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        original_prompt = questions[0].prompt

        tmp = Path(tempfile.mkdtemp())
        for path in Path(app.config["DATA_DIR"]).glob("variant_*.json"):
            shutil.copy(path, tmp / path.name)
        payload = json.loads((tmp / "variant_1.json").read_text(encoding="utf-8"))
        payload["questions"][0]["prompt"] = "ПОДМЕНЁННЫЙ ВОПРОС"
        payload["questions"] = payload["questions"][:-1]          # удаляем последний вопрос
        (tmp / "variant_1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        stats = sync_variants(tmp)
        assert stats["skipped"] >= 1

        variant = Variant.query.filter_by(code="1-в").one()
        assert variant.questions[0].prompt == original_prompt        # формулировка не подменена
        attempt = Attempt.query.filter_by(public_id=public_id).one()
        assert attempt.correct_count == 1                            # ответ на месте
        assert Answer.query.filter_by(attempt_id=attempt.id).count() == 1

        # вопрос без ответов действительно удаляется
        assert Question.query.filter_by(variant_id=variant.id, position=50).count() == 0

        sync_variants(app.config["DATA_DIR"])                        # возвращаем банк на место


def test_name_validation_accepts_diacritics(client, app):
    with app.app_context():
        from app.services import validate_applicant

        for name in ["Müller José", "Ерғали Аружан", "О'Коннор Патрик", "Абдул-Азиз Нурлан", "Ким Виктория"]:
            cleaned, errors = validate_applicant({"full_name": name})
            assert not errors, f"{name} → {errors}"

        for bad in ["Иванов", "И", "Иванов 123", "", "Иванов@Иван"]:
            _cleaned, errors = validate_applicant({"full_name": bad})
            assert "full_name" in errors, f"должно быть отклонено: {bad!r}"


def test_admin_pages_survive_junk_query_params(client, app):
    """Произвольные параметры в строке запроса не должны ломать страницу."""
    with app.app_context():
        public_id, token, _ = start_attempt(client, name="Мусоров Тест")
        client.post(f"/test/{public_id}/submit", data={"csrf_token": token})
        admin_login(client)

        for query in ["?_external=1", "?page=-5", "?page=99999", "?per_page=1000000",
                      "?sort=%3Bdrop&order=hack", "?min_percent=abc&max_percent=..",
                      "?date_from=не-дата&date_to=2026-99-99", "?q=" + "я" * 300]:
            response = client.get("/admin/attempts" + query)
            assert response.status_code == 200, f"{query} → {response.status_code}"
        assert client.get("/admin/attempts/export.csv?_external=1").status_code == 200


def test_in_progress_attempt_detail_renders(client, app):
    """Карточка незавершённой работы открывается и не показывает фиктивный результат."""
    with app.app_context():
        public_id, _, _ = start_attempt(client, name="Ещёидёт Тест")
        admin_login(client)
        body = client.get(f"/admin/attempts/{public_id}").get_data(as_text=True)
        assert "Работа выполняется" in body
        assert "Итоговый результат" not in body
