/* ChemCraft tracker — chèn vào cuối <body> của index.html, lesson.html, lab.html
   Dùng để ghi sự kiện thật lên server (/api/log-event).
   Yêu cầu: đã có window._currentUser (từ Firebase Auth) — không bắt buộc.        */
(function () {
  const API = '/api/log-event';

  function uid() {
    try { return window._currentUser?.uid || ''; } catch (e) { return ''; }
  }

  async function log(type, payload = {}) {
    try {
      await fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, userId: uid(), ...payload }),
        keepalive: true,
      });
    } catch (e) { /* im lặng */ }
  }

  // Public API
  window.ccTrack = log;

  // Tự động: page_view + session_start + heartbeat mỗi 60s
  log('page_view', { path: location.pathname });
  log('session_start', { path: location.pathname, ref: document.referrer || '' });

  setInterval(() => log('heartbeat', { path: location.pathname }), 60_000);

  // Auto-detect theo tên trang
  const p = location.pathname.toLowerCase();

  if (p.includes('lab')) {
    log('lab_open', {});
    // hook global để lesson code gọi: window.ccTrack('lab_step', {step:'add_HCl'})
    // hook global: window.ccTrack('lab_complete', {duration:120, ok:true})
  }

  // Nếu trang lesson expose fbRecordLesson, wrap để log
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
