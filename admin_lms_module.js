/* ═══════════════════════════════════════════════════════════════════════
   CHEMCRAFT ADMIN — Module "Quản lý LMS" (Giáo viên / Lớp học / Premium)
   ---------------------------------------------------------------------
   Bổ sung cho admin.html các mục theo Phần II-IV của bản yêu cầu nâng cấp:
     - Teachers: nâng/hạ quyền teacher, khóa tài khoản, cấp/thu hồi
       "ChemCraft for edu"
     - Classes: xem toàn bộ lớp học trong hệ thống
     - Subscriptions: thống kê Premium / AI usage / Submission usage

   Cách dùng:
     1. Copy file này vào cùng thư mục với admin.html, deploy cùng
        lms_db.py / lms_auth.py / lms_routes.py ở backend.
     2. Trong admin.html thêm dòng script trước </body>:
        <script src="admin_lms_module.js" defer></script>
     3. Thêm 3 mục vào PAGES (admin.html), giống các PAGES['users'] hiện có:
          PAGES['lms-teachers']      = { title:'Giáo viên', sub:'Quản lý quyền & Premium', render: () => window.CC_LMS.renderTeachers() };
          PAGES['lms-classes']       = { title:'Lớp học',   sub:'Toàn bộ lớp học trong hệ thống', render: () => window.CC_LMS.renderClasses() };
          PAGES['lms-subscriptions'] = { title:'Subscriptions', sub:'Thống kê Premium & Usage', render: () => window.CC_LMS.renderStats() };
     4. Thêm 3 mục tương ứng vào sidebar nav (giống cách 'users'/'profile' đã
        được thêm) để admin bấm vào được.
     5. Dùng chung X-Admin-Token với mọi module admin khác — không cần đăng
        nhập thêm.
   ═════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const $  = (sel, root = document) => root.querySelector(sel);
  const esc = s => (s ?? '').toString()
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const fmtDate = ts => !ts ? '—' : new Date(ts * (ts < 1e12 ? 1000 : 1)).toLocaleDateString('vi-VN');

  const adminHeaders = () => ({
    'Content-Type': 'application/json',
    'X-Admin-Token': window.__adminToken || sessionStorage.getItem('cc_admin_token') || '',
  });
  const api = async (url, opts = {}) => {
    const r = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), ...adminHeaders() } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `API ${url} → ${r.status}`);
    return data;
  };

  const toast = (msg) => {
    let host = $('#cc-toast-host');
    if (!host) { host = document.createElement('div'); host.id = 'cc-toast-host'; document.body.appendChild(host); }
    const el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = 'background:#101725;color:#fff;padding:12px 18px;border-radius:10px;font:600 0.85rem "Be Vietnam Pro",sans-serif;margin-top:8px;box-shadow:0 8px 20px rgba(0,0,0,.2);';
    host.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99999;';
    host.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  };

  if (!document.getElementById('cc-lms-module-style')) {
    const style = document.createElement('style');
    style.id = 'cc-lms-module-style';
    style.textContent = `
      .lms-table{width:100%;border-collapse:collapse;font:400 0.86rem 'Be Vietnam Pro',sans-serif;}
      .lms-table th{text-align:left;padding:10px;color:#5B6477;border-bottom:2px solid #E4E9F1;font-weight:700;}
      .lms-table td{padding:10px;border-bottom:1px solid #EEF1F6;}
      .lms-pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:0.72rem;font-weight:700;}
      .lms-pill-gold{background:#FEF3DD;color:#B45309;}
      .lms-pill-cyan{background:#E3F8FB;color:#0891A8;}
      .lms-pill-red{background:#FDEDED;color:#C8312A;}
      .lms-btn{font:700 0.78rem 'Be Vietnam Pro',sans-serif;padding:6px 12px;border:none;border-radius:999px;cursor:pointer;margin-right:6px;}
      .lms-btn-primary{background:linear-gradient(135deg,#4F6BFF,#06B6D4);color:#fff;}
      .lms-btn-danger{background:#FDEDED;color:#C8312A;}
      .lms-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-bottom:20px;}
      .lms-stat-card{background:#fff;border:1px solid #E4E9F1;border-radius:14px;padding:16px;}
      .lms-stat-num{font-weight:900;font-size:1.6rem;color:#101725;}
      .lms-stat-label{font-size:0.8rem;color:#5B6477;margin-top:2px;}
    `;
    document.head.appendChild(style);
  }

  // ── Teachers ────────────────────────────────────────────────────────
  async function renderTeachers() {
    const root = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    root.innerHTML = '<div style="padding:20px;">Đang tải danh sách giáo viên...</div>';
    try {
      const { teachers } = await api('/api/lms/admin/teachers');
      root.innerHTML = `
        <div style="padding:4px 0 16px;">
          <div style="display:flex;gap:8px;margin-bottom:16px;align-items:center;">
            <input id="lms-promote-uid" placeholder="UID học sinh (xem ở tab Quản lý người dùng)" style="flex:1;max-width:340px;padding:9px 12px;border:1.5px solid #E4E9F1;border-radius:10px;font:400 0.85rem 'Be Vietnam Pro',sans-serif;">
            <button class="lms-btn lms-btn-primary" id="lms-promote-btn">Nâng lên Teacher</button>
          </div>
          <table class="lms-table">
            <thead><tr><th>Tên</th><th>Email</th><th>Gói</th><th>Trạng thái</th><th></th></tr></thead>
            <tbody>
              ${teachers.map(t => `
                <tr>
                  <td>${esc(t.displayName || '(chưa đặt tên)')}</td>
                  <td>${esc(t.email || '')}</td>
                  <td>${t.plan === 'chemcraft_for_edu' && t.planStatus === 'active'
                        ? '<span class="lms-pill lms-pill-gold">ChemCraft for edu</span>'
                        : '<span class="lms-pill lms-pill-cyan">Free</span>'}</td>
                  <td>${t.accountDisabled ? '<span class="lms-pill lms-pill-red">Đã khóa</span>' : '<span class="lms-pill" style="background:#E6F8F1;color:#0D9166;">Hoạt động</span>'}</td>
                  <td>
                    ${t.plan === 'chemcraft_for_edu' && t.planStatus === 'active'
                        ? `<button class="lms-btn lms-btn-danger" data-act="revoke" data-uid="${t.uid}">Thu hồi Premium</button>`
                        : `<button class="lms-btn lms-btn-primary" data-act="grant" data-uid="${t.uid}">Cấp ChemCraft for edu</button>`}
                    <button class="lms-btn lms-btn-danger" data-act="toggle" data-uid="${t.uid}" data-disabled="${!!t.accountDisabled}">
                      ${t.accountDisabled ? 'Mở khóa' : 'Khóa tài khoản'}
                    </button>
                    <button class="lms-btn lms-btn-danger" data-act="demote" data-uid="${t.uid}">Hạ về Student</button>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
          ${!teachers.length ? '<div style="padding:30px;text-align:center;color:#8C95A8;">Chưa có giáo viên nào. Nâng quyền từ tab "Quản lý người dùng" để tạo giáo viên đầu tiên.</div>' : ''}
        </div>`;
      $('#lms-promote-btn').onclick = async () => {
        const uid = $('#lms-promote-uid').value.trim();
        if (!uid) return toast('Nhập UID trước đã.');
        try {
          await api(`/api/lms/admin/users/${uid}/role`, { method: 'POST', body: JSON.stringify({ role: 'teacher' }) });
          toast('Đã nâng lên Teacher.'); renderTeachers();
        } catch (e) { toast(e.message); }
      };
      root.querySelectorAll('[data-act="grant"]').forEach(b => b.onclick = () => setPremium(b.dataset.uid, true));
      root.querySelectorAll('[data-act="revoke"]').forEach(b => b.onclick = () => setPremium(b.dataset.uid, false));
      root.querySelectorAll('[data-act="toggle"]').forEach(b => b.onclick = () => toggleActive(b.dataset.uid, b.dataset.disabled === 'true'));
      root.querySelectorAll('[data-act="demote"]').forEach(b => b.onclick = () => demoteTeacher(b.dataset.uid));
    } catch (e) { root.innerHTML = `<div style="padding:20px;color:#C8312A;">${esc(e.message)}</div>`; }
  }

  async function demoteTeacher(uid) {
    if (!confirm('Hạ tài khoản này về Student? Lớp học của họ vẫn giữ nguyên nhưng họ sẽ không quản lý được nữa.')) return;
    try {
      await api(`/api/lms/admin/users/${uid}/role`, { method: 'POST', body: JSON.stringify({ role: 'student' }) });
      toast('Đã hạ về Student.'); renderTeachers();
    } catch (e) { toast(e.message); }
  }

  async function setPremium(uid, active) {
    try {
      await api(`/api/lms/admin/teachers/${uid}/premium`, { method: 'POST', body: JSON.stringify({ active }) });
      toast(active ? 'Đã cấp ChemCraft for edu.' : 'Đã thu hồi Premium.');
      renderTeachers();
    } catch (e) { toast(e.message); }
  }
  async function toggleActive(uid, currentlyDisabled) {
    try {
      await api(`/api/lms/admin/users/${uid}/active`, { method: 'POST', body: JSON.stringify({ active: currentlyDisabled }) });
      toast(currentlyDisabled ? 'Đã mở khóa tài khoản.' : 'Đã khóa tài khoản.');
      renderTeachers();
    } catch (e) { toast(e.message); }
  }

  // ── Classes ─────────────────────────────────────────────────────────
  async function renderClasses() {
    const root = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    root.innerHTML = '<div style="padding:20px;">Đang tải danh sách lớp học...</div>';
    try {
      const { classes } = await api('/api/lms/admin/classes');
      root.innerHTML = `
        <table class="lms-table">
          <thead><tr><th>Tên lớp</th><th>Khối</th><th>Mã lớp</th><th>Số học sinh</th><th>Trạng thái</th></tr></thead>
          <tbody>
            ${classes.map(c => `
              <tr>
                <td>${esc(c.name)}</td><td>${c.grade}</td><td>${esc(c.joinCode)}</td>
                <td>${(c.studentIds || []).length}</td>
                <td>${c.status === 'active' ? '<span class="lms-pill" style="background:#E6F8F1;color:#0D9166;">Đang hoạt động</span>' : '<span class="lms-pill lms-pill-red">Đã đóng</span>'}</td>
              </tr>`).join('')}
          </tbody>
        </table>
        ${!classes.length ? '<div style="padding:30px;text-align:center;color:#8C95A8;">Chưa có lớp học nào trong hệ thống.</div>' : ''}
      `;
    } catch (e) { root.innerHTML = `<div style="padding:20px;color:#C8312A;">${esc(e.message)}</div>`; }
  }

  // ── Stats ───────────────────────────────────────────────────────────
  async function renderStats() {
    const root = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    root.innerHTML = '<div style="padding:20px;">Đang tải thống kê...</div>';
    try {
      const s = await api('/api/lms/admin/stats');
      root.innerHTML = `
        <div class="lms-stat-grid">
          <div class="lms-stat-card"><div class="lms-stat-num">${s.teacherCount}</div><div class="lms-stat-label">Giáo viên</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.premiumTeacherCount}</div><div class="lms-stat-label">Giáo viên Premium</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.classCount}</div><div class="lms-stat-label">Lớp học</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.studentCount}</div><div class="lms-stat-label">Học sinh (trong lớp)</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.submissionCount}</div><div class="lms-stat-label">Lượt nộp bài</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.testAttemptCount}</div><div class="lms-stat-label">Lượt làm bài kiểm tra</div></div>
          <div class="lms-stat-card"><div class="lms-stat-num">${s.livestreamCount}</div><div class="lms-stat-label">Buổi livestream</div></div>
        </div>
      `;
    } catch (e) { root.innerHTML = `<div style="padding:20px;color:#C8312A;">${esc(e.message)}</div>`; }
  }

  window.CC_LMS = { renderTeachers, renderClasses, renderStats };
})();
