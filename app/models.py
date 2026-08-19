"""Модели данных платформы тестирования."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint, Index
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    """Наивный UTC — единый формат хранения времени в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Variant(db.Model):
    """Вариант теста (1-в, 2-в, ...)."""

    __tablename__ = "variants"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    questions = db.relationship(
        "Question",
        back_populates="variant",
        cascade="all, delete-orphan",
        order_by="Question.position",
        lazy="selectin",
    )
    attempts = db.relationship("Attempt", back_populates="variant", lazy="dynamic")

    @property
    def question_count(self) -> int:
        return len(self.questions)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Variant {self.code}>"


class Question(db.Model):
    """Вопрос теста."""

    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("variant_id", "position", name="uq_question_variant_position"),)

    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id", ondelete="CASCADE"), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    section = db.Column(db.String(120), nullable=False, default="")
    passage = db.Column(db.Text)
    prompt = db.Column(db.Text, nullable=False)

    variant = db.relationship("Variant", back_populates="questions")
    options = db.relationship(
        "Option",
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="Option.letter",
        lazy="selectin",
    )

    @property
    def correct_option(self):
        for option in self.options:
            if option.is_correct:
                return option
        return None

    @property
    def correct_letter(self) -> str:
        option = self.correct_option
        return option.letter if option else ""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Question {self.variant_id}#{self.position}>"


class Option(db.Model):
    """Вариант ответа."""

    __tablename__ = "options"
    __table_args__ = (UniqueConstraint("question_id", "letter", name="uq_option_question_letter"),)

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    letter = db.Column(db.String(2), nullable=False)
    text = db.Column(db.Text, nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)

    question = db.relationship("Question", back_populates="options")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Option {self.letter}>"


class Attempt(db.Model):
    """Попытка прохождения теста абитуриентом."""

    __tablename__ = "attempts"

    STATUS_IN_PROGRESS = "in_progress"
    STATUS_SUBMITTED = "submitted"
    STATUS_EXPIRED = "expired"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False, default=lambda: uuid.uuid4().hex)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False)

    full_name = db.Column(db.String(160), nullable=False)
    full_name_norm = db.Column(db.String(160), nullable=False, default="")
    phone = db.Column(db.String(40), default="")
    email = db.Column(db.String(160), default="")
    school = db.Column(db.String(200), default="")
    # Единая строка для поиска в админке: приводится к нижнему регистру средствами
    # Python, т.к. SQLite lower() не понимает кириллицу.
    search_text = db.Column(db.String(700), nullable=False, default="")

    status = db.Column(db.String(20), nullable=False, default=STATUS_IN_PROGRESS)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_activity_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer)

    total_questions = db.Column(db.Integer, nullable=False, default=0)
    correct_count = db.Column(db.Integer, nullable=False, default=0)
    wrong_count = db.Column(db.Integer, nullable=False, default=0)
    unanswered_count = db.Column(db.Integer, nullable=False, default=0)
    percent = db.Column(db.Float, nullable=False, default=0.0)
    grade = db.Column(db.String(60), default="")
    level = db.Column(db.String(16), default="")

    ip_address = db.Column(db.String(64), default="")
    user_agent = db.Column(db.String(300), default="")

    variant = db.relationship("Variant", back_populates="attempts")
    answers = db.relationship(
        "Answer", back_populates="attempt", cascade="all, delete-orphan", lazy="selectin"
    )

    def refresh_search_text(self) -> None:
        """Пересобирает поисковую строку по данным абитуриента."""
        parts = [
            self.full_name or "",
            self.phone or "",
            self.email or "",
            self.school or "",
            self.public_id or "",
        ]
        self.full_name_norm = (self.full_name or "").lower()
        self.search_text = " ".join(parts).lower()

    @property
    def is_finished(self) -> bool:
        return self.status in (self.STATUS_SUBMITTED, self.STATUS_EXPIRED)

    @property
    def answered_count(self) -> int:
        return sum(1 for a in self.answers if a.option_id is not None)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attempt {self.public_id} {self.full_name}>"


Index("ix_attempts_full_name_norm", Attempt.full_name_norm)
Index("ix_attempts_search_text", Attempt.search_text)
Index("ix_attempts_started_at", Attempt.started_at)
Index("ix_attempts_status", Attempt.status)


class Answer(db.Model):
    """Ответ абитуриента на конкретный вопрос."""

    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id", name="uq_answer_attempt_question"),)

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("options.id", ondelete="SET NULL"))
    letter = db.Column(db.String(2), default="")
    is_correct = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempt = db.relationship("Attempt", back_populates="answers")
    question = db.relationship("Question", lazy="selectin")
    option = db.relationship("Option", lazy="selectin")


class AdminUser(db.Model):
    """Администратор системы."""

    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class Setting(db.Model):
    """Настройки платформы (ключ-значение)."""

    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default="")
