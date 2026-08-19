/* Прохождение теста: автосохранение ответов, таймер, навигатор, отправка. */
(function () {
  "use strict";

  const root = document.getElementById("test-root");
  if (!root) return;

  const ANSWER_URL = root.dataset.answerUrl;
  const EXPIRE_URL = root.dataset.expireUrl;
  const STATE_URL = root.dataset.stateUrl;
  const CSRF = root.dataset.csrf;
  const TOTAL = parseInt(root.dataset.total, 10) || 0;

  let secondsLeft = parseInt(root.dataset.secondsLeft, 10) || 0;
  let submitting = false;      // идёт отправка формы — предупреждение при уходе не нужно
  let navigating = false;      // страницу перезагружает сам скрипт
  let submitRequested = false; // пользователь подтвердил отправку и не отменил её

  const els = {
    answered: document.getElementById("answered-count"),
    progress: document.getElementById("progress-bar"),
    timer: document.getElementById("timer"),
    timerValue: document.getElementById("timer-value"),
    status: document.getElementById("save-status"),
    statusText: document.querySelector("[data-save-text]"),
    form: document.getElementById("test-form"),
    modal: document.getElementById("submit-modal"),
    modalAnswered: document.getElementById("modal-answered"),
    modalWarning: document.getElementById("modal-warning"),
  };

  /* ------------------------------------------------------------------ */
  /* Индикатор сохранения                                                */
  /* ------------------------------------------------------------------ */

  const TONES = {
    saving: ["bg-amber-50", "text-amber-700", "ring-amber-200"],
    saved: ["bg-emerald-50", "text-emerald-700", "ring-emerald-200"],
    error: ["bg-rose-50", "text-rose-700", "ring-rose-200"],
  };

  let hideTimer = null;

  function setStatus(kind, text) {
    if (!els.status) return;
    Object.values(TONES).flat().forEach((cls) => els.status.classList.remove(cls));
    TONES[kind].forEach((cls) => els.status.classList.add(cls));
    els.status.classList.remove("hidden");
    els.status.classList.add("flex");
    if (els.statusText) els.statusText.textContent = text;
    clearTimeout(hideTimer);
    if (kind === "saved") {
      hideTimer = setTimeout(() => {
        els.status.classList.add("hidden");
        els.status.classList.remove("flex");
      }, 2500);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Состояние ответов                                                   */
  /* ------------------------------------------------------------------ */

  function answeredCount() {
    return document.querySelectorAll("#test-form input[type=radio]:checked").length;
  }

  function refreshProgress() {
    const count = answeredCount();
    if (els.answered) els.answered.textContent = count;
    if (els.progress) els.progress.style.width = TOTAL ? (count * 100) / TOTAL + "%" : "0%";
    if (els.modalAnswered) els.modalAnswered.textContent = count;
    return count;
  }

  function markCard(card) {
    const checked = card.querySelector("input[type=radio]:checked");
    const badge = card.querySelector("[data-badge]");
    const clearBtn = card.querySelector("[data-clear]");
    const nav = document.querySelector('[data-nav="' + card.dataset.questionId + '"]');

    if (badge) {
      badge.classList.toggle("bg-brand-600", !!checked);
      badge.classList.toggle("text-white", !!checked);
      badge.classList.toggle("bg-slate-100", !checked);
      badge.classList.toggle("text-slate-600", !checked);
    }
    if (clearBtn) clearBtn.classList.toggle("hidden", !checked);
    if (nav) nav.classList.toggle("nav-answered", !!checked);
  }

  /* ------------------------------------------------------------------ */
  /* Отправка ответа на сервер (с повторами)                             */
  /* ------------------------------------------------------------------ */

  const queue = new Map(); // questionId -> letter (последнее значение побеждает)
  let flushing = false;
  let retryAttempt = 0;    // счётчик повторов живёт между вызовами
  let retryTimer = null;   // единственная цепочка повторов

  function goTo(url) {
    navigating = true;
    if (url) {
      window.location.href = url;
    } else {
      window.location.reload();
    }
  }

  function scheduleRetry() {
    const delay = Math.min(15000, 1000 * Math.pow(2, retryAttempt));
    retryAttempt += 1;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(flush, delay);
  }

  function enqueue(questionId, letter) {
    queue.set(String(questionId), letter);
    flush();
  }

  async function flush() {
    if (flushing || queue.size === 0) return;
    flushing = true;
    setStatus("saving", "Сохранение…");

    const [questionId, letter] = queue.entries().next().value;

    try {
      const response = await fetch(ANSWER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify({ question_id: Number(questionId), letter: letter }),
        credentials: "same-origin",
      });
      const data = await response.json().catch(() => ({}));

      if (response.status === 409) {
        // время истекло или работа уже сдана — уходим на страницу результата
        goTo();
        return;
      }
      if (!response.ok || !data.ok) throw new Error(data.message || "save failed");

      // удаляем из очереди только если ответ не изменили, пока шёл запрос
      if (queue.get(questionId) === letter) queue.delete(questionId);
      if (typeof data.seconds_left === "number") secondsLeft = data.seconds_left;
      retryAttempt = 0;
      clearTimeout(retryTimer);
      flushing = false;
      if (queue.size) {
        flush();
      } else {
        setStatus("saved", "Сохранено");
      }
    } catch (error) {
      flushing = false;
      setStatus("error", "Нет связи — повтор…");
      scheduleRetry();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Обработчики ответов                                                 */
  /* ------------------------------------------------------------------ */

  document.querySelectorAll(".question-card").forEach((card) => {
    markCard(card);

    card.addEventListener("change", (event) => {
      if (event.target.type !== "radio") return;
      markCard(card);
      refreshProgress();
      enqueue(card.dataset.questionId, event.target.value);
    });

    const clearBtn = card.querySelector("[data-clear]");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        card.querySelectorAll("input[type=radio]").forEach((input) => (input.checked = false));
        markCard(card);
        refreshProgress();
        enqueue(card.dataset.questionId, "");
      });
    }
  });

  document.querySelectorAll("[data-nav]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.querySelector(link.getAttribute("href"));
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        target.classList.add("ring-2", "ring-brand-300");
        setTimeout(() => target.classList.remove("ring-2", "ring-brand-300"), 1200);
      }
    });
  });

  /* ------------------------------------------------------------------ */
  /* Таймер                                                              */
  /* ------------------------------------------------------------------ */

  function formatTime(total) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  async function expireNow() {
    if (submitting) return;
    submitting = true;
    try {
      const response = await fetch(EXPIRE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        credentials: "same-origin",
      });
      const data = await response.json().catch(() => ({}));
      goTo(data.redirect || window.location.pathname);
    } catch (error) {
      goTo();
    }
  }

  function tick() {
    if (secondsLeft <= 0) {
      if (els.timerValue) els.timerValue.textContent = "00:00";
      expireNow();
      return;
    }
    secondsLeft -= 1;
    if (els.timerValue) els.timerValue.textContent = formatTime(secondsLeft);
    if (els.timer) {
      const critical = secondsLeft <= 300;
      els.timer.classList.toggle("bg-rose-600", critical);
      els.timer.classList.toggle("bg-slate-900", !critical);
      els.timer.classList.toggle("animate-pulse", secondsLeft <= 60);
    }
  }

  if (secondsLeft > 0) {
    if (els.timerValue) els.timerValue.textContent = formatTime(secondsLeft);
    setInterval(tick, 1000);
  } else if (els.timer) {
    els.timer.classList.add("hidden");
  }

  /* Синхронизация с сервером раз в минуту: точное время и защита от расхождений */
  setInterval(async () => {
    if (submitting) return;
    try {
      const response = await fetch(STATE_URL, { credentials: "same-origin" });
      const data = await response.json();
      if (data.ok) {
        if (data.status !== "in_progress") {
          goTo();
          return;
        }
        if (typeof data.seconds_left === "number") secondsLeft = data.seconds_left;
      }
    } catch (error) {
      /* сеть недоступна — продолжаем локальный отсчёт */
    }
  }, 60000);

  /* ------------------------------------------------------------------ */
  /* Отправка теста                                                      */
  /* ------------------------------------------------------------------ */

  function openModal() {
    const count = refreshProgress();
    const left = TOTAL - count;
    if (els.modalWarning) {
      if (left > 0) {
        els.modalWarning.textContent =
          "Без ответа осталось " + left + " " + pluralize(left) + ". Они будут засчитаны как неверные.";
        els.modalWarning.classList.remove("hidden");
      } else {
        els.modalWarning.textContent = "";
        els.modalWarning.classList.add("hidden");
      }
    }
    els.modal.classList.remove("hidden");
    els.modal.classList.add("flex");
  }

  function pluralize(n) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return "вопрос";
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "вопроса";
    return "вопросов";
  }

  function closeModal() {
    submitRequested = false;
    const confirmBtn = document.getElementById("submit-confirm");
    confirmBtn.disabled = false;
    confirmBtn.textContent = "Да, завершить";
    els.modal.classList.add("hidden");
    els.modal.classList.remove("flex");
  }

  document.getElementById("submit-open").addEventListener("click", openModal);
  document.getElementById("submit-cancel").addEventListener("click", closeModal);
  els.modal.addEventListener("click", (event) => {
    if (event.target === els.modal) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.modal.classList.contains("hidden")) closeModal();
  });

  document.getElementById("submit-confirm").addEventListener("click", async () => {
    // дожидаемся, пока последние ответы уйдут на сервер (не больше 20 секунд)
    const confirmBtn = document.getElementById("submit-confirm");
    submitRequested = true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Отправка…";
    for (let i = 0; i < 80 && queue.size > 0; i++) {
      if (!submitRequested) return;                 // пользователь передумал
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!submitRequested) return;
    // даже если очередь не разошлась, ответы уйдут вместе с формой:
    // сервер сохраняет их из полей формы при завершении теста
    submitting = true;
    els.form.submit();
  });

  window.addEventListener("beforeunload", (event) => {
    if (submitting || navigating) return;
    event.preventDefault();
    event.returnValue = "";
  });

  // возврат кнопкой «назад» не должен показывать уже сданный тест из кэша
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });

  refreshProgress();
})();
