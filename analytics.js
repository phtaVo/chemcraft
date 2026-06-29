/* ════════════════════════════════════════════════════════════════
   CHEMCRAFT ANALYTICS — module ghi log sự kiện dùng chung
   Yêu cầu: trang đã set window._fbAuth = auth (Firebase Auth instance)
   trước khi gọi ChemAnalytics.logEvent.
   ════════════════════════════════════════════════════════════════ */
window.ChemAnalytics = (function () {
  var ENDPOINT = '/api/log-event';

  async function getToken() {
    try {
      var auth = window._fbAuth;
      if (auth && auth.currentUser) {
        return await auth.currentUser.getIdToken();
      }
    } catch (e) {
      console.warn('[ChemAnalytics] Không lấy được token:', e);
    }
    return null;
  }

  /**
   * Ghi 1 sự kiện hành vi của user. Chỉ ghi khi đã đăng nhập (im lặng bỏ qua nếu chưa).
   * @param {string} type - một trong: page_view, lesson_start, lesson_complete,
   *                         quiz_answer, quiz_submit, lab_action, ai_question, login, logout
   * @param {object} data - { lessonId, questionId, correct, score, timeSpentSec, meta }
   */
  async function logEvent(type, data) {
    var token = await getToken();
    if (!token) return; // chưa đăng nhập → không có gì để gắn dữ liệu, bỏ qua êm

    var payload = Object.assign({ type: type }, data || {});
    try {
      // fire-and-forget — không chặn UX của người dùng
      fetch(ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + token,
        },
        body: JSON.stringify(payload),
      }).catch(function (e) {
        console.warn('[ChemAnalytics] Gửi event lỗi (bỏ qua, không ảnh hưởng UX):', e);
      });
    } catch (e) {
      console.warn('[ChemAnalytics] Lỗi logEvent:', e);
    }
  }

  return { logEvent: logEvent };
})();
