/* ChemCraft tracker v2 — chèn vào cuối <body> của index.html, lesson.html, lab.html
   Ghi sự kiện thật lên server (/api/log-event).
   Yêu cầu: đã có window._currentUser (từ Firebase Auth) — không bắt buộc.

   ĐIỂM MỚI so với v1:
   - quiz_start / quiz_answer / quiz_complete: trước đây khai báo ở backend
     nhưng KHÔNG BAO GIỜ được gọi. Nay có API rõ ràng: window.ccQuiz.*
   - lab_reaction_attempt / lab_reaction_result / lab_close: trước đây
     lab_step/lab_complete/lab_error không bao giờ được gọi. Nay có
     window.ccLab.* để lab.html gọi tại đúng điểm phản ứng được thực thi.
   - lab_open giờ trả về sessionId — PHẢI lưu lại và gửi kèm mọi
     lab_reaction_result / lab_close tiếp theo, nếu không Lab Analytics
     sẽ không nối được các lượt chạy phản ứng vào đúng phiên.
*/
(function () {
  const API = '/api/log-event';

  function uid() {
    try { return window._currentUser?.uid || ''; } catch (e) { return ''; }
  }

  async function log(type, payload = {}) {
    try {
      const res = await fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, userId: uid(), ...payload }),
        keepalive: true,
      });
      return await res.json().catch(() => null);
    } catch (e) { return null; }
  }

  window.ccTrack = log;

  // ── Auto: page_view + session_start + heartbeat ───────────────────────
  log('page_view', { path: location.pathname });
  log('session_start', { path: location.pathname, ref: document.referrer || '' });
  setInterval(() => log('heartbeat', { path: location.pathname }), 60_000);

  const p = location.pathname.toLowerCase();

  // ── Lab session lifecycle ───────────────────────────────────────────────
  // lab.html MUST call window.ccLab.reactionResult(...) every time a
  // reaction is attempted — this is the data Lab Analytics is built on.
  // Nothing downstream works until this is wired in.
  let _labSessionId = null;
  let _labOpenedAt = null;

  window.ccLab = {
    async open() {
      _labOpenedAt = Date.now();
      const res = await log('lab_open', {});
      _labSessionId = res && res.sessionId ? res.sessionId : null;
      return _labSessionId;
    },
    // Call when the user starts building a reaction (chemicals+equipment chosen)
    attempt(reactionEq, chemicals, equipment) {
      log('lab_reaction_attempt', {
        sessionId: _labSessionId,
        reactionEq, chemicals, equipment,
      });
    },
    // Call once the reaction engine knows the outcome
    result(reactionEq, outcome, opts = {}) {
      log('lab_reaction_result', {
        sessionId: _labSessionId,
        reactionEq,
        outcome,              // 'success' | 'failure' | 'error'
        chemicals: JSON.stringify(opts.chemicals || []),
        equipment: JSON.stringify(opts.equipment || []),
        errorReason: opts.errorReason || null,
        durationSec: opts.durationSec ?? null,
      });
    },
    close() {
      if (!_labOpenedAt) return;
      log('lab_close', {
        sessionId: _labSessionId,
        durationSec: Math.round((Date.now() - _labOpenedAt) / 1000),
      });
    },
  };

  if (p.includes('lab')) {
    window.ccLab.open();
    window.addEventListener('beforeunload', () => window.ccLab.close());
  }

  // ── Quiz lifecycle ───────────────────────────────────────────────────────
  // lesson.html's quiz code MUST call these three in sequence — see
  // INTEGRATION_NOTES.md §1 for exact call sites in the existing functions
  // (startQuiz / checkAnswer / finishQuiz equivalents).
  let _quizAttemptId = null;
  let _quizStartedAt = null;

  window.ccQuiz = {
    async start(totalQuestions) {
      _quizStartedAt = Date.now();
      const res = await log('quiz_start', { totalQuestions });
      _quizAttemptId = res && res.attemptId ? res.attemptId : null;
      return _quizAttemptId;
    },
    answer(questionId, questionText, correct, retryCount = 0) {
      log('quiz_answer', {
        attemptId: _quizAttemptId,
        questionId, questionText: (questionText || '').slice(0, 200),
        correct, retryCount,
      });
    },
    complete(correctCount, totalQuestions) {
      log('quiz_complete', {
        attemptId: _quizAttemptId,
        correctCount, totalQuestions,
        durationSec: _quizStartedAt ? Math.round((Date.now() - _quizStartedAt) / 1000) : null,
      });
    },
  };

  // ── lesson_complete: wrap fbRecordLesson as before ─────────────────────
  const origRecord = window.fbRecordLesson;
  if (typeof origRecord === 'function') {
    window.fbRecordLesson = async function (entry) {
      try {
        log('lesson_complete', {
          lessonId: entry?.id || entry?.lessonId || '',
          title:    entry?.title || '',
          score:    entry?.score ?? null,
        });
      } catch (e) {}
      return origRecord.apply(this, arguments);
    };
  }
})();
