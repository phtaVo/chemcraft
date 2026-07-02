/* ═══════════════════════════════════════════════════════════════════════
   CHEMCRAFT ADMIN — Module "Quản lý người dùng" + "Hồ sơ học sinh"
   ---------------------------------------------------------------------
   Cách dùng:
     1. Copy file này vào cùng thư mục với admin.html
     2. Trong admin.html thêm dòng script trước </body>:
        <script src="admin_users_module.js" defer></script>
     3. Trong admin.html, thêm import Firebase mở rộng (xem hướng dẫn
        admin_patch_instructions.md)
     4. Trong PAGES của admin.html, thay 2 mục 'users' và 'profile' bằng:
          users:   { title:'Quản lý người dùng', sub:'…',
                     render: () => window.CC_Users.render() },
          profile: { title:'Hồ sơ học sinh',      sub:'…',
                     render: (uid) => window.CC_Profile.render(uid) },
     5. Không cần thay đổi gì khác — module tự inject CSS.
   Yêu cầu backend: đã append server_admin_patch.py vào server.py
   ═════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── Helpers ─────────────────────────────────────────────────────────
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = s => (s ?? '').toString()
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const fmtTime = ts => {
    if (!ts) return '—';
    const d = new Date(ts * (ts < 1e12 ? 1000 : 1));
    return d.toLocaleString('vi-VN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  };
  const fmtDate = ts => {
    if (!ts) return '—';
    const d = new Date(ts * (ts < 1e12 ? 1000 : 1));
    return d.toLocaleDateString('vi-VN');
  };
  const relTime = ts => {
    if (!ts) return 'chưa từng';
    const now = Date.now() / 1000;
    const t = ts > 1e12 ? ts / 1000 : ts;
    const d = now - t;
    if (d < 60)      return 'vừa xong';
    if (d < 3600)    return `${Math.floor(d / 60)} phút trước`;
    if (d < 86400)   return `${Math.floor(d / 3600)} giờ trước`;
    if (d < 604800)  return `${Math.floor(d / 86400)} ngày trước`;
    if (d < 2592000) return `${Math.floor(d / 604800)} tuần trước`;
    return `${Math.floor(d / 2592000)} tháng trước`;
  };
  const secToHuman = s => {
    if (!s || s < 60) return `${Math.max(0, Math.round(s || 0))}s`;
    if (s < 3600) return `${Math.round(s / 60)} phút`;
    if (s < 86400) return `${(s / 3600).toFixed(1)} giờ`;
    return `${(s / 86400).toFixed(1)} ngày`;
  };
  const initials = (name, email) => {
    const src = (name || email || '?').trim();
    const parts = src.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    return src.slice(0, 2).toUpperCase();
  };
  const avatarColor = key => {
    const colors = [
      'linear-gradient(135deg,#06B6D4,#4F6BFF)',
      'linear-gradient(135deg,#F59E0B,#EC4899)',
      'linear-gradient(135deg,#8B5CF6,#4F6BFF)',
      'linear-gradient(135deg,#10B981,#06B6D4)',
      'linear-gradient(135deg,#EF4444,#F59E0B)',
      'linear-gradient(135deg,#EC4899,#8B5CF6)',
    ];
    let h = 0;
    for (const c of (key || '')) h = (h * 31 + c.charCodeAt(0)) & 0x7fffffff;
    return colors[h % colors.length];
  };

  const adminHeaders = () => ({
    'Content-Type': 'application/json',
    'X-Admin-Token': window.__adminToken || sessionStorage.getItem('cc_admin_token') || '',
  });

  const api = async (url, opts = {}) => {
    const r = await fetch(url, {
      ...opts,
      headers: { ...(opts.headers || {}), ...adminHeaders() },
    });
    if (!r.ok) throw new Error(`API ${url} → ${r.status}`);
    return r.json();
  };

  // ── Toast ───────────────────────────────────────────────────────────
  const toast = (msg, kind = 'ok') => {
    let host = $('#cc-toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'cc-toast-host';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `cc-toast cc-toast-${kind}`;
    el.innerHTML = `<i class="fa-solid ${
      kind === 'err' ? 'fa-circle-exclamation'
      : kind === 'warn' ? 'fa-triangle-exclamation' : 'fa-circle-check'
    }"></i><span>${esc(msg)}</span>`;
    host.appendChild(el);
    setTimeout(() => { el.classList.add('gone'); setTimeout(() => el.remove(), 300); }, 3200);
  };

  // ── Modal ───────────────────────────────────────────────────────────
  const modal = (title, bodyHtml, opts = {}) => new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'cc-modal-wrap';
    wrap.innerHTML = `
      <div class="cc-modal">
        <div class="cc-modal-head">
          <h3>${esc(title)}</h3>
          <button class="cc-modal-x" aria-label="Đóng"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div class="cc-modal-body">${bodyHtml}</div>
        <div class="cc-modal-foot">
          <button class="cc-btn cc-btn-ghost" data-act="cancel">${esc(opts.cancelLabel || 'Hủy')}</button>
          <button class="cc-btn cc-btn-primary" data-act="ok">${esc(opts.okLabel || 'Xác nhận')}</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    requestAnimationFrame(() => wrap.classList.add('show'));
    const close = v => { wrap.classList.remove('show'); setTimeout(() => wrap.remove(), 200); resolve(v); };
    wrap.querySelector('.cc-modal-x').onclick = () => close(null);
    wrap.querySelector('[data-act="cancel"]').onclick = () => close(null);
    wrap.querySelector('[data-act="ok"]').onclick = () => {
      const form = wrap.querySelector('form');
      if (form && !form.reportValidity()) return;
      const data = {};
      if (form) new FormData(form).forEach((v, k) => (data[k] = v));
      close(data);
    };
    wrap.addEventListener('click', e => { if (e.target === wrap) close(null); });
  });

  // ── Firestore user store ────────────────────────────────────────────
  const FS = () => {
    if (!window._fbReady || !window._fbDb || !window._fbFns) {
      throw new Error('Firestore chưa sẵn sàng — vui lòng chờ 1 giây rồi thử lại.');
    }
    return { db: window._fbDb, fns: window._fbFns };
  };

  async function loadAllUsers() {
    const { db, fns } = FS();
    const { collection, getDocs, query, orderBy, limit } =
      fns;
    // Không dùng orderBy bắt buộc (createdAt có thể thiếu ở doc cũ)
    let snap;
    try {
      snap = await getDocs(query(collection(db, 'users'), orderBy('createdAt', 'desc'), limit(2000)));
    } catch (e) {
      snap = await getDocs(collection(db, 'users'));
    }
    const arr = [];
    snap.forEach(d => arr.push({ id: d.id, ...d.data() }));
    return arr;
  }

  async function updateUserDoc(uid, patch) {
    const { db, fns } = FS();
    const { doc, updateDoc, setDoc, getDoc } = fns;
    if (!updateDoc) throw new Error('Thiếu updateDoc — kiểm tra import trong admin.html');
    const ref = doc(db, 'users', uid);
    const s = await getDoc(ref);
    if (!s.exists()) await setDoc(ref, patch, { merge: true });
    else await updateDoc(ref, patch);
  }

  async function softDeleteUser(uid) {
    await updateUserDoc(uid, { deletedAt: Date.now(), status: 'deleted' });
  }
  async function restoreUser(uid) {
    await updateUserDoc(uid, { deletedAt: null, status: 'active' });
  }
  async function setUserLock(uid, locked) {
    await updateUserDoc(uid, { locked: !!locked, status: locked ? 'locked' : 'active' });
  }
  async function setUserRole(uid, role) {
    await updateUserDoc(uid, { role });
  }

  // ── Style injection ─────────────────────────────────────────────────
  if (!$('#cc-users-style')) {
    const s = document.createElement('style');
    s.id = 'cc-users-style';
    s.textContent = `
    /* KPI grid */
    .ccu-kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:18px;}
    .ccu-kpi{background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:16px 18px; display:flex; align-items:center; gap:14px; box-shadow:var(--sh-1);}
    .ccu-kpi-ico{width:44px; height:44px; border-radius:12px; display:flex; align-items:center; justify-content:center; color:#fff; font-size:1.05rem; flex-shrink:0;}
    .ccu-kpi-lbl{font-size:0.78rem; color:var(--fg-soft); font-weight:600; letter-spacing:0.02em;}
    .ccu-kpi-val{font-size:1.4rem; font-weight:800; color:var(--fg); letter-spacing:-0.02em;}
    .ccu-kpi-sub{font-size:0.72rem; color:var(--fg-mute); font-weight:500;}

    /* Toolbar */
    .ccu-toolbar{background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:14px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:14px; box-shadow:var(--sh-1);}
    .ccu-search{flex:1; min-width:220px; display:flex; align-items:center; gap:8px; background:var(--bg-2); border:1px solid var(--line); border-radius:var(--r-md); padding:9px 12px;}
    .ccu-search input{flex:1; border:none; outline:none; background:transparent; color:var(--fg); font-family:inherit; font-size:0.9rem;}
    .ccu-search i{color:var(--fg-mute);}
    .ccu-filters{display:flex; gap:8px; flex-wrap:wrap;}
    .ccu-chip{padding:7px 12px; border-radius:var(--r-pill); border:1px solid var(--line); background:var(--bg-2); color:var(--fg-soft); font-size:0.8rem; font-weight:600; cursor:pointer; transition:.15s;}
    .ccu-chip.on{background:linear-gradient(135deg,var(--cyan),var(--indigo)); color:#fff; border-color:transparent;}
    .ccu-chip:hover{color:var(--fg);}
    .ccu-chip.on:hover{color:#fff; filter:brightness(1.05);}
    .ccu-actions{display:flex; gap:8px; margin-left:auto;}

    /* Buttons */
    .cc-btn{display:inline-flex; align-items:center; gap:7px; padding:9px 14px; border-radius:var(--r-md); font-weight:700; font-size:0.83rem; cursor:pointer; border:1px solid transparent; transition:.15s; font-family:inherit;}
    .cc-btn-primary{background:linear-gradient(135deg,var(--cyan),var(--indigo)); color:#fff; box-shadow:var(--sh-cyan);}
    .cc-btn-primary:hover{transform:translateY(-1px); filter:brightness(1.05);}
    .cc-btn-ghost{background:var(--bg-2); color:var(--fg); border-color:var(--line);}
    .cc-btn-ghost:hover{background:var(--surface-2);}
    .cc-btn-danger{background:var(--red-l); color:var(--red-d); border-color:var(--red);}
    .cc-btn-danger:hover{background:var(--red); color:#fff;}
    .cc-btn-sm{padding:6px 10px; font-size:0.76rem;}

    /* Table */
    .ccu-table-wrap{background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); overflow:hidden; box-shadow:var(--sh-2);}
    .ccu-table{width:100%; border-collapse:collapse; font-size:0.85rem;}
    .ccu-table th{background:var(--surface-2); color:var(--fg-soft); font-weight:700; padding:12px 14px; text-align:left; font-size:0.74rem; letter-spacing:0.04em; text-transform:uppercase; border-bottom:1px solid var(--line); cursor:pointer; user-select:none; white-space:nowrap;}
    .ccu-table th.sortable::after{content:'\\f0dc'; font-family:'Font Awesome 6 Free'; font-weight:900; margin-left:6px; opacity:.35; font-size:0.7rem;}
    .ccu-table th.sort-asc::after{content:'\\f0de'; opacity:1; color:var(--cyan);}
    .ccu-table th.sort-desc::after{content:'\\f0dd'; opacity:1; color:var(--cyan);}
    .ccu-table td{padding:12px 14px; border-bottom:1px solid var(--border-soft); color:var(--fg); vertical-align:middle;}
    .ccu-table tr:last-child td{border-bottom:none;}
    .ccu-table tr:hover td{background:var(--bg-2);}
    .ccu-name{display:flex; align-items:center; gap:11px; min-width:0;}
    .ccu-ava{width:36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:0.78rem; flex-shrink:0;}
    .ccu-name-txt{min-width:0;}
    .ccu-name-txt .n{font-weight:700; color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:200px;}
    .ccu-name-txt .e{font-size:0.75rem; color:var(--fg-mute); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:220px;}
    .ccu-badge{display:inline-flex; align-items:center; gap:5px; padding:3px 9px; border-radius:var(--r-pill); font-size:0.7rem; font-weight:700; letter-spacing:0.02em;}
    .ccu-bg-student{background:var(--indigo-l); color:var(--indigo-d);}
    .ccu-bg-teacher{background:var(--purple-l); color:var(--purple-d);}
    .ccu-bg-admin{background:var(--gold-l); color:var(--gold-d);}
    .ccu-status{display:inline-flex; align-items:center; gap:6px; font-size:0.78rem; font-weight:600;}
    .ccu-dot{width:8px; height:8px; border-radius:50%; background:var(--fg-mute);}
    .ccu-dot.online{background:var(--green); box-shadow:0 0 0 3px rgba(16,185,129,0.2); animation:ccu-pulse 1.6s ease-in-out infinite;}
    .ccu-dot.locked{background:var(--red);}
    .ccu-dot.unverified{background:var(--gold);}
    @keyframes ccu-pulse{50%{box-shadow:0 0 0 5px rgba(16,185,129,0);}}
    .ccu-row-actions{display:flex; gap:6px; opacity:.4; transition:.15s;}
    .ccu-table tr:hover .ccu-row-actions{opacity:1;}
    .ccu-icobtn{width:30px; height:30px; border-radius:8px; border:1px solid var(--line); background:var(--bg-2); color:var(--fg-soft); display:flex; align-items:center; justify-content:center; cursor:pointer; transition:.15s; font-size:0.78rem;}
    .ccu-icobtn:hover{background:var(--surface-2); color:var(--fg);}
    .ccu-icobtn.danger:hover{background:var(--red); color:#fff; border-color:var(--red);}

    .ccu-empty{padding:60px 20px; text-align:center; color:var(--fg-mute);}
    .ccu-empty i{font-size:2.6rem; opacity:.35; margin-bottom:14px;}

    /* Pagination */
    .ccu-pager{display:flex; align-items:center; justify-content:space-between; padding:14px 16px; background:var(--surface-2); border-top:1px solid var(--line); font-size:0.82rem; color:var(--fg-soft);}
    .ccu-pager-btns{display:flex; gap:6px;}
    .ccu-pager button{padding:6px 11px; border-radius:8px; border:1px solid var(--line); background:var(--surface); color:var(--fg); font-family:inherit; font-weight:600; cursor:pointer; transition:.15s;}
    .ccu-pager button:disabled{opacity:.4; cursor:not-allowed;}
    .ccu-pager button.active{background:linear-gradient(135deg,var(--cyan),var(--indigo)); color:#fff; border-color:transparent;}

    /* Modal */
    .cc-modal-wrap{position:fixed; inset:0; background:rgba(11,18,32,0.55); backdrop-filter:blur(4px); z-index:5000; display:flex; align-items:center; justify-content:center; padding:20px; opacity:0; transition:opacity .2s;}
    .cc-modal-wrap.show{opacity:1;}
    .cc-modal{background:var(--surface); border-radius:var(--r-xl); box-shadow:var(--sh-4); width:100%; max-width:520px; max-height:90vh; display:flex; flex-direction:column; transform:translateY(10px) scale(.98); transition:transform .22s cubic-bezier(.16,1,.3,1);}
    .cc-modal-wrap.show .cc-modal{transform:none;}
    .cc-modal-head{display:flex; align-items:center; justify-content:space-between; padding:18px 22px; border-bottom:1px solid var(--line);}
    .cc-modal-head h3{font-size:1.05rem; font-weight:800; color:var(--fg);}
    .cc-modal-x{width:32px; height:32px; border-radius:8px; border:none; background:var(--bg-2); color:var(--fg-soft); cursor:pointer; display:flex; align-items:center; justify-content:center;}
    .cc-modal-x:hover{background:var(--surface-2); color:var(--fg);}
    .cc-modal-body{padding:20px 22px; overflow-y:auto;}
    .cc-modal-foot{display:flex; justify-content:flex-end; gap:8px; padding:14px 22px; border-top:1px solid var(--line);}
    .cc-modal-body .field{margin-bottom:14px;}
    .cc-modal-body label{display:block; font-size:0.78rem; font-weight:700; color:var(--fg-soft); margin-bottom:6px; text-transform:uppercase; letter-spacing:0.04em;}
    .cc-modal-body input, .cc-modal-body select, .cc-modal-body textarea{width:100%; padding:11px 13px; border:1.5px solid var(--line); border-radius:var(--r-md); font-family:inherit; font-size:0.9rem; color:var(--fg); background:var(--bg-2); outline:none;}
    .cc-modal-body input:focus, .cc-modal-body select:focus, .cc-modal-body textarea:focus{border-color:var(--cyan); box-shadow:0 0 0 3px rgba(6,182,212,0.15);}

    /* Toast */
    #cc-toast-host{position:fixed; right:24px; top:24px; z-index:6000; display:flex; flex-direction:column; gap:10px;}
    .cc-toast{display:flex; align-items:center; gap:10px; padding:12px 16px; background:var(--surface); border-radius:var(--r-md); box-shadow:var(--sh-3); border-left:4px solid var(--green); color:var(--fg); font-weight:600; font-size:0.86rem; min-width:260px; animation:ccu-in .25s cubic-bezier(.16,1,.3,1);}
    .cc-toast.gone{opacity:0; transform:translateX(20px); transition:.25s;}
    .cc-toast-err{border-left-color:var(--red);}
    .cc-toast-err i{color:var(--red);}
    .cc-toast-warn{border-left-color:var(--gold);}
    .cc-toast-warn i{color:var(--gold);}
    .cc-toast-ok i{color:var(--green);}
    @keyframes ccu-in{from{opacity:0; transform:translateX(20px);} to{opacity:1; transform:none;}}

    /* Profile page */
    .ccp-head{background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:22px 24px; display:flex; gap:20px; align-items:center; box-shadow:var(--sh-2); margin-bottom:18px; position:relative;}
    .ccp-back{position:absolute; left:14px; top:14px; background:var(--bg-2); border:1px solid var(--line); border-radius:8px; padding:6px 10px; font-size:0.78rem; font-weight:700; color:var(--fg-soft); cursor:pointer;}
    .ccp-back:hover{color:var(--fg); background:var(--surface-2);}
    .ccp-ava{width:76px; height:76px; border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:1.6rem; flex-shrink:0;}
    .ccp-info h2{font-size:1.35rem; font-weight:800; color:var(--fg); letter-spacing:-0.02em;}
    .ccp-info .em{font-size:0.86rem; color:var(--fg-soft); font-weight:500; margin-top:4px;}
    .ccp-tags{display:flex; gap:8px; flex-wrap:wrap; margin-top:9px;}
    .ccp-tag{font-size:0.74rem; padding:4px 10px; border-radius:var(--r-pill); background:var(--bg-2); color:var(--fg-soft); font-weight:600;}
    .ccp-head-actions{margin-left:auto; display:flex; gap:8px;}
    .ccp-grid{display:grid; grid-template-columns:repeat(12, 1fr); gap:16px;}
    .ccp-card{background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:18px 20px; box-shadow:var(--sh-1);}
    .ccp-card h4{font-size:0.78rem; text-transform:uppercase; letter-spacing:0.06em; color:var(--fg-soft); font-weight:800; margin-bottom:14px; display:flex; align-items:center; gap:8px;}
    .ccp-card h4 i{color:var(--cyan);}
    .ccp-c-6{grid-column:span 6;}
    .ccp-c-4{grid-column:span 4;}
    .ccp-c-8{grid-column:span 8;}
    .ccp-c-12{grid-column:span 12;}
    @media (max-width:960px){ .ccp-c-6,.ccp-c-4,.ccp-c-8{grid-column:span 12;} }
    .ccp-canvas{position:relative; height:230px;}
    .ccp-canvas-lg{position:relative; height:280px;}

    /* Heatmap */
    .ccp-heatmap{display:grid; grid-template-columns:auto repeat(24, 1fr); gap:3px; font-size:0.65rem;}
    .ccp-heatmap .h-lbl{color:var(--fg-mute); font-weight:600; padding-right:6px; text-align:right; align-self:center;}
    .ccp-heatmap .h-cell{aspect-ratio:1; border-radius:3px; background:var(--surface-2); position:relative;}
    .ccp-heatmap .h-hdr{color:var(--fg-mute); font-weight:600; text-align:center;}

    /* Timeline */
    .ccp-timeline{position:relative; padding-left:22px;}
    .ccp-timeline::before{content:''; position:absolute; left:8px; top:6px; bottom:6px; width:2px; background:var(--line);}
    .ccp-t-item{position:relative; padding:8px 0 14px;}
    .ccp-t-item::before{content:''; position:absolute; left:-19px; top:12px; width:12px; height:12px; border-radius:50%; background:var(--surface); border:2px solid var(--cyan); box-shadow:0 0 0 3px rgba(6,182,212,0.15);}
    .ccp-t-item.c-green::before{border-color:var(--green); box-shadow:0 0 0 3px rgba(16,185,129,0.15);}
    .ccp-t-item.c-red::before{border-color:var(--red); box-shadow:0 0 0 3px rgba(239,68,68,0.15);}
    .ccp-t-item.c-indigo::before{border-color:var(--indigo); box-shadow:0 0 0 3px rgba(79,107,255,0.15);}
    .ccp-t-item.c-purple::before{border-color:var(--purple); box-shadow:0 0 0 3px rgba(139,92,246,0.15);}
    .ccp-t-item.c-gold::before{border-color:var(--gold); box-shadow:0 0 0 3px rgba(245,158,11,0.15);}
    .ccp-t-item.c-text-m::before{border-color:var(--fg-mute); box-shadow:0 0 0 3px rgba(0,0,0,0.06);}
    .ccp-t-lbl{font-weight:700; font-size:0.86rem; color:var(--fg);}
    .ccp-t-time{font-size:0.74rem; color:var(--fg-mute); font-weight:600;}
    .ccp-t-extra{font-size:0.78rem; color:var(--fg-soft); margin-top:2px;}
    .ccp-t-scroll{max-height:520px; overflow-y:auto;}
    .ccp-mini{display:flex; align-items:baseline; gap:8px;}
    .ccp-mini .v{font-size:1.7rem; font-weight:800; color:var(--fg); letter-spacing:-0.02em;}
    .ccp-mini .u{font-size:0.78rem; color:var(--fg-mute); font-weight:600;}
    .ccp-listcard li{display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px dashed var(--border-soft); font-size:0.84rem;}
    .ccp-listcard li:last-child{border-bottom:none;}
    .ccp-listcard li b{font-weight:700; color:var(--fg);}
    .ccp-listcard li span{color:var(--fg-mute); font-weight:600;}
    `;
    document.head.appendChild(s);
  }

  // ════════════════════════════════════════════════════════════════════
  //  MODULE 1 — USER MANAGEMENT
  // ════════════════════════════════════════════════════════════════════
  const UState = {
    users: [],
    activity: {},     // uid -> {last_seen, online, sessions, total_time_sec, days_used}
    online: new Set(),
    sort: { key: 'last_seen', dir: 'desc' },
    search: '',
    filters: new Set(['all']),
    page: 1,
    pageSize: 20,
  };

  const CC_Users = {
    async render() {
      const host = $('#page-root');
      host.innerHTML = `
        <div class="ccu-kpis" id="ccu-kpis">
          ${[1,2,3,4,5,6,7].map(() =>
            `<div class="ccu-kpi"><div class="ccu-kpi-ico" style="background:var(--surface-2)"></div>
             <div><div class="ccu-kpi-lbl">Đang tải…</div><div class="ccu-kpi-val">—</div></div></div>`
          ).join('')}
        </div>
        <div class="ccu-toolbar">
          <div class="ccu-search"><i class="fa-solid fa-magnifying-glass"></i>
            <input id="ccu-q" placeholder="Tìm theo tên, email, UID, trường, lớp…" autocomplete="off">
          </div>
          <div class="ccu-filters" id="ccu-filters">
            ${[
              ['all',        'Tất cả',       'fa-list'],
              ['online',     'Đang online',  'fa-circle', 'green'],
              ['active',     'Hoạt động',    'fa-bolt'],
              ['inactive',   'Không hoạt động','fa-moon'],
              ['locked',     'Đã khóa',      'fa-lock'],
              ['unverified', 'Chưa xác thực','fa-envelope'],
              ['role:student','Học sinh',    'fa-user-graduate'],
              ['role:teacher','Giáo viên',   'fa-chalkboard-user'],
              ['role:admin', 'Quản trị',     'fa-user-shield'],
            ].map(([k,l,i,c]) =>
              `<div class="ccu-chip${k==='all'?' on':''}" data-f="${k}">
                <i class="fa-solid ${i}" ${c?`style="color:var(--${c})"`:''}></i> ${esc(l)}
              </div>`
            ).join('')}
          </div>
          <div class="ccu-actions">
            <button class="cc-btn cc-btn-ghost" id="ccu-export">
              <i class="fa-solid fa-file-export"></i> Xuất CSV
            </button>
            <button class="cc-btn cc-btn-ghost" id="ccu-import">
              <i class="fa-solid fa-file-import"></i> Import
            </button>
            <button class="cc-btn cc-btn-primary" id="ccu-new">
              <i class="fa-solid fa-plus"></i> Tạo tài khoản
            </button>
          </div>
        </div>
        <div class="ccu-table-wrap">
          <div id="ccu-table-slot"></div>
          <div class="ccu-pager" id="ccu-pager"></div>
        </div>
      `;

      $('#ccu-q').addEventListener('input', e => { UState.search = e.target.value.trim().toLowerCase(); UState.page = 1; renderTable(); });
      $$('#ccu-filters .ccu-chip').forEach(c => c.onclick = () => {
        const k = c.dataset.f;
        if (k === 'all') { UState.filters = new Set(['all']); }
        else {
          UState.filters.delete('all');
          if (UState.filters.has(k)) UState.filters.delete(k);
          else UState.filters.add(k);
          if (!UState.filters.size) UState.filters.add('all');
        }
        $$('#ccu-filters .ccu-chip').forEach(x => x.classList.toggle('on', UState.filters.has(x.dataset.f)));
        UState.page = 1; renderTable();
      });
      $('#ccu-new').onclick = () => openUserForm(null);
      $('#ccu-export').onclick = exportCsv;
      $('#ccu-import').onclick = importCsv;

      renderTable(true); // trạng thái loading trước
      try {
        UState.users = await loadAllUsers();
      } catch (e) {
        console.error(e);
        $('#ccu-table-slot').innerHTML = `
          <div class="ccu-empty"><i class="fa-solid fa-triangle-exclamation"></i>
            <div>Không đọc được collection <code>users</code> từ Firestore.</div>
            <div style="font-size:.8rem; margin-top:6px;">Kiểm tra Security Rules cho phép admin đọc, hoặc chờ Firestore init xong rồi thử lại.</div>
          </div>`;
        renderKpis(); return;
      }
      await refreshActivity();
      renderKpis();
      renderTable();
      // Polling online mỗi 30s
      clearInterval(UState._poll);
      UState._poll = setInterval(async () => {
        try { await refreshOnlineOnly(); renderKpis(); renderTable(); } catch(e){}
      }, 30000);
    },
  };

  async function refreshActivity() {
    const uids = UState.users.map(u => u.id);
    if (!uids.length) return;
    try {
      const chunks = [];
      for (let i = 0; i < uids.length; i += 300) chunks.push(uids.slice(i, i + 300));
      const results = await Promise.all(chunks.map(c =>
        api('/api/admin-user-activity', {
          method: 'POST',
          body: JSON.stringify({ uids: c }),
        })
      ));
      UState.activity = {};
      results.forEach(r => Object.assign(UState.activity, r.activity || {}));
      UState.online = new Set(
        Object.entries(UState.activity).filter(([, v]) => v.online).map(([k]) => k)
      );
    } catch (e) { console.warn('activity load:', e); }
  }
  async function refreshOnlineOnly() {
    const r = await api('/api/admin-online-uids');
    UState.online = new Set((r.online || []).map(x => x.uid));
    for (const uid of Object.keys(UState.activity)) {
      UState.activity[uid].online = UState.online.has(uid);
    }
  }

  function renderKpis() {
    const users = UState.users;
    const now = Date.now();
    const wk = 7 * 86400e3;
    const today = new Date(); today.setHours(0,0,0,0);
    const c = {
      total:    users.length,
      online:   UState.online.size,
      newWeek:  users.filter(u => (u.createdAt?.seconds ? u.createdAt.seconds*1000 : (u.createdAt || 0)) > now - wk).length,
      todayAct: Object.values(UState.activity).filter(a => (a.last_seen||0)*1000 >= today.getTime()).length,
      teacher:  users.filter(u => (u.role || 'student') === 'teacher').length,
      student:  users.filter(u => (u.role || 'student') === 'student').length,
      locked:   users.filter(u => u.locked || u.status === 'locked').length,
    };
    const cards = [
      ['Tổng tài khoản',      c.total,    'fa-users',          'linear-gradient(135deg,var(--indigo),#6B82FF)'],
      ['Đang online',          c.online,   'fa-signal',        'linear-gradient(135deg,var(--green),#34D399)'],
      ['Mới trong tuần',       '+'+c.newWeek,'fa-user-plus',   'linear-gradient(135deg,var(--cyan),var(--indigo))'],
      ['Hoạt động hôm nay',    c.todayAct, 'fa-bolt',          'linear-gradient(135deg,var(--gold),var(--pink))'],
      ['Giáo viên',            c.teacher,  'fa-chalkboard-user','linear-gradient(135deg,var(--purple),#B784FF)'],
      ['Học sinh',             c.student,  'fa-user-graduate', 'linear-gradient(135deg,var(--cyan-d),var(--cyan))'],
      ['Tài khoản bị khóa',    c.locked,   'fa-lock',          'linear-gradient(135deg,var(--red),#F87171)'],
    ];
    $('#ccu-kpis').innerHTML = cards.map(([lbl, val, ico, bg]) => `
      <div class="ccu-kpi">
        <div class="ccu-kpi-ico" style="background:${bg}"><i class="fa-solid ${ico}"></i></div>
        <div>
          <div class="ccu-kpi-lbl">${esc(lbl)}</div>
          <div class="ccu-kpi-val">${esc(String(val))}</div>
        </div>
      </div>`).join('');
  }

  function statusOf(u) {
    if (u.deletedAt) return 'deleted';
    if (u.locked || u.status === 'locked') return 'locked';
    if (u.emailVerified === false) return 'unverified';
    const act = UState.activity[u.id];
    if (act?.online) return 'online';
    if (act && (Date.now()/1000 - (act.last_seen||0)) < 14*86400) return 'active';
    return 'inactive';
  }

  function passFilters(u) {
    if (UState.filters.has('all')) return true;
    const st = statusOf(u);
    const role = u.role || 'student';
    for (const f of UState.filters) {
      if (f === 'online' && st === 'online') return true;
      if (f === 'active' && (st === 'active' || st === 'online')) return true;
      if (f === 'inactive' && st === 'inactive') return true;
      if (f === 'locked' && st === 'locked') return true;
      if (f === 'unverified' && st === 'unverified') return true;
      if (f.startsWith('role:') && f.slice(5) === role) return true;
    }
    return false;
  }

  function passSearch(u) {
    if (!UState.search) return true;
    const q = UState.search;
    return [u.displayName, u.name, u.email, u.id, u.school, u.grade, u.class]
      .some(v => (v || '').toString().toLowerCase().includes(q));
  }

  function sortValue(u, key) {
    const act = UState.activity[u.id] || {};
    switch (key) {
      case 'name':      return (u.displayName || u.name || u.email || '').toLowerCase();
      case 'createdAt': return u.createdAt?.seconds ? u.createdAt.seconds : (u.createdAt || 0);
      case 'last_seen': return act.last_seen || 0;
      case 'activity':  return act.total_time_sec || 0;
      case 'role':      return u.role || 'student';
      case 'days':      return act.days_used || 0;
      default: return 0;
    }
  }

  function renderTable(loading = false) {
    const slot = $('#ccu-table-slot');
    if (loading) {
      slot.innerHTML = `<div class="ccu-empty"><i class="fa-solid fa-spinner fa-spin"></i><div>Đang tải danh sách…</div></div>`;
      $('#ccu-pager').innerHTML = '';
      return;
    }
    let rows = UState.users.filter(u => passSearch(u) && passFilters(u));
    const { key, dir } = UState.sort;
    rows.sort((a, b) => {
      const av = sortValue(a, key), bv = sortValue(b, key);
      if (av < bv) return dir === 'asc' ? -1 : 1;
      if (av > bv) return dir === 'asc' ? 1 : -1;
      return 0;
    });

    const total = rows.length;
    const totalPages = Math.max(1, Math.ceil(total / UState.pageSize));
    if (UState.page > totalPages) UState.page = totalPages;
    const start = (UState.page - 1) * UState.pageSize;
    const pageRows = rows.slice(start, start + UState.pageSize);

    if (!pageRows.length) {
      slot.innerHTML = `<div class="ccu-empty"><i class="fa-solid fa-inbox"></i>
        <div>Không có tài khoản nào khớp bộ lọc.</div></div>`;
    } else {
      const th = (key, label, sortable = true) =>
        `<th class="${sortable ? 'sortable' : ''} ${UState.sort.key===key ? 'sort-'+UState.sort.dir : ''}" data-k="${key}">${label}</th>`;
      slot.innerHTML = `
        <table class="ccu-table">
          <thead><tr>
            ${th('name','Người dùng')}
            ${th('role','Vai trò')}
            <th>Trường / Lớp</th>
            ${th('createdAt','Ngày tạo')}
            ${th('last_seen','Đăng nhập gần nhất')}
            ${th('days','Ngày dùng')}
            <th>Trạng thái</th>
            <th style="text-align:right">Thao tác</th>
          </tr></thead>
          <tbody>
            ${pageRows.map(u => rowHtml(u)).join('')}
          </tbody>
        </table>`;

      $$('#ccu-table-slot th.sortable').forEach(th => th.onclick = () => {
        const k = th.dataset.k;
        if (UState.sort.key === k) UState.sort.dir = UState.sort.dir === 'asc' ? 'desc' : 'asc';
        else { UState.sort.key = k; UState.sort.dir = 'desc'; }
        renderTable();
      });
      $$('#ccu-table-slot tr[data-uid]').forEach(tr => {
        tr.querySelector('.ccu-name').onclick = () => window.navigateTo && window.navigateTo('profile', tr.dataset.uid);
      });
      $$('#ccu-table-slot [data-act]').forEach(btn => btn.onclick = e => {
        e.stopPropagation();
        handleRowAction(btn.dataset.act, btn.dataset.uid);
      });
    }

    // pager
    const pageBtn = n =>
      `<button ${n===UState.page?'class="active"':''} data-p="${n}">${n}</button>`;
    const nearby = [];
    for (let i = Math.max(1, UState.page-2); i <= Math.min(totalPages, UState.page+2); i++) nearby.push(i);
    $('#ccu-pager').innerHTML = `
      <div>Hiển thị <b>${total ? start+1 : 0}–${Math.min(start+UState.pageSize, total)}</b> / <b>${total}</b> tài khoản</div>
      <div class="ccu-pager-btns">
        <button data-p="prev" ${UState.page===1?'disabled':''}><i class="fa-solid fa-chevron-left"></i></button>
        ${nearby.map(pageBtn).join('')}
        <button data-p="next" ${UState.page>=totalPages?'disabled':''}><i class="fa-solid fa-chevron-right"></i></button>
      </div>`;
    $$('#ccu-pager button[data-p]').forEach(b => b.onclick = () => {
      const p = b.dataset.p;
      if (p === 'prev') UState.page--;
      else if (p === 'next') UState.page++;
      else UState.page = +p;
      renderTable();
    });
  }

  function rowHtml(u) {
    const act = UState.activity[u.id] || {};
    const st = statusOf(u);
    const stMap = {
      online:     ['online',   'Online'],
      active:     ['',         'Hoạt động'],
      inactive:   ['',         'Không hoạt động'],
      locked:     ['locked',   'Đã khóa'],
      unverified: ['unverified','Chưa xác thực'],
      deleted:    ['locked',   'Đã xóa'],
    };
    const role = u.role || 'student';
    const roleLabel = { student:'Học sinh', teacher:'Giáo viên', admin:'Quản trị' }[role] || role;
    return `
      <tr data-uid="${esc(u.id)}">
        <td>
          <div class="ccu-name" style="cursor:pointer">
            <div class="ccu-ava" style="background:${avatarColor(u.id)}">${esc(initials(u.displayName||u.name, u.email))}</div>
            <div class="ccu-name-txt">
              <div class="n">${esc(u.displayName || u.name || '(chưa đặt tên)')}</div>
              <div class="e">${esc(u.email || '')}${u.id ? ' · '+esc(u.id.slice(0,8)) : ''}</div>
            </div>
          </div>
        </td>
        <td><span class="ccu-badge ccu-bg-${role}">${esc(roleLabel)}</span></td>
        <td>
          <div style="font-weight:600">${esc(u.school || '—')}</div>
          <div style="font-size:.75rem; color:var(--fg-mute)">${esc(u.class || u.grade || '')}</div>
        </td>
        <td>${fmtDate(u.createdAt?.seconds || u.createdAt)}</td>
        <td>
          <div style="font-weight:600">${relTime(act.last_seen)}</div>
          <div style="font-size:.72rem; color:var(--fg-mute)">${act.last_seen ? fmtTime(act.last_seen) : ''}</div>
        </td>
        <td><b>${act.days_used || 0}</b> ngày</td>
        <td><span class="ccu-status"><span class="ccu-dot ${stMap[st][0]}"></span>${esc(stMap[st][1])}</span></td>
        <td>
          <div class="ccu-row-actions" style="justify-content:flex-end">
            <button class="ccu-icobtn" data-act="edit"   data-uid="${esc(u.id)}" title="Chỉnh sửa"><i class="fa-solid fa-pen"></i></button>
            <button class="ccu-icobtn" data-act="reset"  data-uid="${esc(u.id)}" title="Reset mật khẩu"><i class="fa-solid fa-key"></i></button>
            <button class="ccu-icobtn" data-act="${u.locked?'unlock':'lock'}" data-uid="${esc(u.id)}" title="${u.locked?'Mở khóa':'Khóa'}"><i class="fa-solid fa-${u.locked?'unlock':'lock'}"></i></button>
            <button class="ccu-icobtn danger" data-act="delete" data-uid="${esc(u.id)}" title="Xóa"><i class="fa-solid fa-trash"></i></button>
          </div>
        </td>
      </tr>`;
  }

  async function handleRowAction(act, uid) {
    const u = UState.users.find(x => x.id === uid);
    if (!u) return;
    try {
      if (act === 'edit') { openUserForm(u); return; }
      if (act === 'reset') {
        const ok = await modal('Reset mật khẩu',
          `<p style="font-size:.9rem; line-height:1.5;">Gửi email đặt lại mật khẩu tới <b>${esc(u.email)}</b>?<br>
           Người dùng sẽ nhận link từ Firebase Auth để đặt lại.</p>`,
          { okLabel: 'Gửi email' });
        if (!ok) return;
        if (!window._fbAuth) {
          toast('Cần import sendPasswordResetEmail vào admin.html (xem hướng dẫn).', 'warn'); return;
        }
        const { sendPasswordResetEmail } = window._fbFns;
        if (!sendPasswordResetEmail) { toast('Thiếu sendPasswordResetEmail trong _fbFns.', 'warn'); return; }
        await sendPasswordResetEmail(window._fbAuth, u.email);
        toast('Đã gửi email đặt lại mật khẩu.');
        return;
      }
      if (act === 'lock' || act === 'unlock') {
        await setUserLock(uid, act === 'lock');
        u.locked = act === 'lock';
        toast(act === 'lock' ? 'Đã khóa tài khoản.' : 'Đã mở khóa.');
        renderTable(); renderKpis(); return;
      }
      if (act === 'delete') {
        const ok = await modal('Xóa tài khoản',
          `<p style="font-size:.9rem; line-height:1.5;">Bạn có chắc muốn xóa mềm tài khoản <b>${esc(u.displayName||u.email)}</b>?<br>
           Tài khoản sẽ bị đánh dấu <code>deleted</code> và ẩn khỏi danh sách chính. Có thể khôi phục sau.</p>`,
          { okLabel: 'Xóa' });
        if (!ok) return;
        await softDeleteUser(uid);
        u.deletedAt = Date.now(); u.status = 'deleted';
        toast('Đã xóa tài khoản.');
        renderTable(); renderKpis(); return;
      }
    } catch (e) {
      console.error(e);
      toast(e.message || 'Thao tác thất bại.', 'err');
    }
  }

  async function openUserForm(u) {
    const isNew = !u;
    const data = await modal(isNew ? 'Tạo tài khoản mới' : 'Chỉnh sửa tài khoản', `
      <form>
        <div class="field"><label>Họ và tên</label>
          <input name="displayName" value="${esc(u?.displayName || u?.name || '')}" required></div>
        <div class="field"><label>Email</label>
          <input name="email" type="email" value="${esc(u?.email || '')}" ${isNew?'required':'disabled'}></div>
        <div class="field"><label>Vai trò</label>
          <select name="role">
            ${['student','teacher','admin'].map(r =>
              `<option value="${r}" ${((u?.role||'student')===r)?'selected':''}>${
                r==='student'?'Học sinh':r==='teacher'?'Giáo viên':'Quản trị viên'
              }</option>`).join('')}
          </select></div>
        <div class="field"><label>Trường</label>
          <input name="school" value="${esc(u?.school || '')}"></div>
        <div class="field"><label>Lớp</label>
          <input name="class" value="${esc(u?.class || u?.grade || '')}"></div>
        ${isNew ? `
        <div class="field" style="background:var(--gold-l); border:1px dashed var(--gold); padding:12px; border-radius:10px; font-size:.82rem; color:var(--gold-d);">
          <i class="fa-solid fa-circle-info"></i> Lưu ý: hệ thống chỉ tạo <b>hồ sơ Firestore</b>.
          Tài khoản xác thực Firebase Auth cần được người dùng tự đăng ký, hoặc tạo qua Firebase Console.
        </div>` : ''}
      </form>`, { okLabel: isNew ? 'Tạo' : 'Lưu thay đổi' });
    if (!data) return;

    try {
      if (isNew) {
        const { db, fns } = FS();
        const { doc, setDoc, serverTimestamp } = fns;
        const uid = 'user_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
        await setDoc(doc(db, 'users', uid), {
          displayName: data.displayName,
          email: data.email,
          role: data.role,
          school: data.school || '',
          class: data.class || '',
          createdAt: serverTimestamp(),
          lessonHistory: [],
          lessonProgress: {},
          status: 'active',
          manuallyCreated: true,
        });
        UState.users.unshift({ id: uid, ...data, createdAt: { seconds: Date.now()/1000 } });
        toast('Đã tạo hồ sơ tài khoản.');
      } else {
        await updateUserDoc(u.id, {
          displayName: data.displayName,
          role:  data.role,
          school: data.school || '',
          class:  data.class || '',
        });
        Object.assign(u, {
          displayName: data.displayName, role: data.role,
          school: data.school, class: data.class,
        });
        toast('Đã lưu thay đổi.');
      }
      renderTable(); renderKpis();
    } catch (e) {
      toast(e.message || 'Không lưu được.', 'err');
    }
  }

  function exportCsv() {
    const rows = [['uid','email','displayName','role','school','class','createdAt','lastSeen','daysUsed','status']];
    UState.users.forEach(u => {
      const a = UState.activity[u.id] || {};
      rows.push([
        u.id, u.email || '', u.displayName || u.name || '',
        u.role || 'student', u.school || '', u.class || u.grade || '',
        fmtDate(u.createdAt?.seconds || u.createdAt),
        a.last_seen ? new Date(a.last_seen*1000).toISOString() : '',
        a.days_used || 0, statusOf(u),
      ]);
    });
    const csv = rows.map(r => r.map(v => `"${String(v ?? '').replace(/"/g,'""')}"`).join(',')).join('\n');
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `chemcraft-users-${new Date().toISOString().slice(0,10)}.csv`; a.click();
    URL.revokeObjectURL(url);
    toast('Đã xuất danh sách CSV.');
  }

  async function importCsv() {
    const data = await modal('Import tài khoản', `
      <div style="font-size:.86rem; line-height:1.55; color:var(--fg-soft); margin-bottom:12px;">
        Dán CSV với các cột: <code>email, displayName, role, school, class</code>.
        Mỗi dòng 1 tài khoản. UID sẽ được sinh tự động.
      </div>
      <form>
        <div class="field"><label>Nội dung CSV</label>
          <textarea name="csv" rows="10" placeholder="email,displayName,role,school,class
hs01@example.com,Nguyễn Văn A,student,THPT Nguyễn Trãi,10A1"></textarea>
        </div>
      </form>`, { okLabel: 'Import' });
    if (!data?.csv) return;
    const lines = data.csv.trim().split(/\r?\n/);
    const header = lines[0].split(',').map(x => x.trim().toLowerCase());
    const idx = k => header.indexOf(k);
    let ok = 0, fail = 0;
    const { db, fns } = FS();
    const { doc, setDoc, serverTimestamp } = fns;
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(',').map(x => x.trim().replace(/^"|"$/g,''));
      const email = cols[idx('email')]; if (!email) { fail++; continue; }
      const uid = 'user_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36) + i;
      try {
        await setDoc(doc(db, 'users', uid), {
          email,
          displayName: cols[idx('displayname')] || '',
          role: cols[idx('role')] || 'student',
          school: cols[idx('school')] || '',
          class: cols[idx('class')] || '',
          createdAt: serverTimestamp(),
          lessonHistory: [], lessonProgress: {},
          status: 'active', manuallyCreated: true, imported: true,
        });
        ok++;
      } catch (e) { fail++; }
    }
    toast(`Đã import ${ok} tài khoản${fail?` (${fail} lỗi)`:''}.`, fail?'warn':'ok');
    UState.users = await loadAllUsers();
    await refreshActivity();
    renderTable(); renderKpis();
  }

  window.CC_Users = CC_Users;

  // ════════════════════════════════════════════════════════════════════
  //  MODULE 2 — STUDENT PROFILE
  // ════════════════════════════════════════════════════════════════════
  const CC_Profile = {
    async render(uid) {
      const host = $('#page-root');
      if (!uid) {
        host.innerHTML = `<div class="ccu-empty" style="padding:80px 20px">
          <i class="fa-solid fa-id-card"></i>
          <div>Chọn một người dùng từ trang <b>Quản lý người dùng</b> để xem hồ sơ chi tiết.</div>
          <button class="cc-btn cc-btn-primary" style="margin-top:16px"
            onclick="window.navigateTo && window.navigateTo('users')">
            <i class="fa-solid fa-users"></i> Đi tới danh sách
          </button></div>`;
        return;
      }
      host.innerHTML = `<div class="ccu-empty"><i class="fa-solid fa-spinner fa-spin"></i>
        <div>Đang tải hồ sơ…</div></div>`;

      // 1. Firestore user doc
      let uDoc = {};
      try {
        const { db, fns } = FS();
        const s = await fns.getDoc(fns.doc(db, 'users', uid));
        if (s.exists()) uDoc = { id: uid, ...s.data() };
      } catch (e) { console.warn('firestore profile:', e); }

      // 2. Backend aggregation
      let p = {};
      try { p = await api(`/api/admin-user-profile?uid=${encodeURIComponent(uid)}&days=30`); }
      catch (e) { toast('Không lấy được dữ liệu hoạt động: '+e.message, 'err'); }

      renderProfile(host, uid, uDoc, p);
    }
  };

  function renderProfile(host, uid, u, p) {
    const summary = p.summary || {};
    const role = u.role || 'student';
    const roleLabel = { student:'Học sinh', teacher:'Giáo viên', admin:'Quản trị' }[role] || role;
    const online = summary.online;
    const stLabel = online ? 'Đang online' :
      (u.locked ? 'Đã khóa' :
      (summary.last_seen ? 'Không online' : 'Chưa từng đăng nhập'));

    host.innerHTML = `
      <button class="ccp-back" onclick="window.navigateTo && window.navigateTo('users')">
        <i class="fa-solid fa-arrow-left"></i> Danh sách
      </button>

      <div class="ccp-head">
        <div class="ccp-ava" style="background:${avatarColor(uid)}">${esc(initials(u.displayName||u.name, u.email))}</div>
        <div class="ccp-info">
          <h2>${esc(u.displayName || u.name || '(chưa đặt tên)')}</h2>
          <div class="em">${esc(u.email || '')} · <code>${esc(uid.slice(0,12))}</code></div>
          <div class="ccp-tags">
            <span class="ccu-badge ccu-bg-${role}">${esc(roleLabel)}</span>
            ${u.school ? `<span class="ccp-tag"><i class="fa-solid fa-school"></i> ${esc(u.school)}</span>` : ''}
            ${u.class||u.grade ? `<span class="ccp-tag"><i class="fa-solid fa-users"></i> ${esc(u.class||u.grade)}</span>` : ''}
            <span class="ccp-tag"><i class="fa-solid fa-calendar-plus"></i> Tạo: ${fmtDate(u.createdAt?.seconds || u.createdAt)}</span>
            <span class="ccp-tag"><span class="ccu-dot ${online?'online':(u.locked?'locked':'')}"></span> ${esc(stLabel)}</span>
          </div>
        </div>
        <div class="ccp-head-actions">
          <button class="cc-btn cc-btn-ghost" id="ccp-edit"><i class="fa-solid fa-pen"></i> Sửa</button>
          <button class="cc-btn cc-btn-ghost" id="ccp-reset"><i class="fa-solid fa-key"></i> Reset MK</button>
          <button class="cc-btn cc-btn-${u.locked?'primary':'danger'}" id="ccp-lock">
            <i class="fa-solid fa-${u.locked?'unlock':'lock'}"></i> ${u.locked?'Mở khóa':'Khóa'}
          </button>
        </div>
      </div>

      <div class="ccu-kpis">
        ${[
          ['fa-book-bookmark','Bài học hoàn thành', (p.lesson?.completed||0), `Đang học: ${p.lesson?.in_progress||0}`, 'linear-gradient(135deg,var(--indigo),#6B82FF)'],
          ['fa-clock','Tổng thời gian sử dụng', secToHuman(summary.total_time_sec||0), `Trung bình ${secToHuman((summary.total_time_sec||0)/Math.max(1,summary.days_used||1))}/ngày`, 'linear-gradient(135deg,var(--cyan),var(--indigo))'],
          ['fa-list-check','Quiz đã làm', (p.quiz?.done||0), `Điểm TB: ${p.quiz?.avg??'—'}`, 'linear-gradient(135deg,var(--gold),var(--pink))'],
          ['fa-vial-circle-check','Thí nghiệm Lab 3D', (p.lab?.completed||0)+'/'+(p.lab?.opened||0), `Hoàn thành ${p.lab?.completion||0}%`, 'linear-gradient(135deg,var(--purple),#B784FF)'],
          ['fa-robot','Số lần hỏi AI', (p.ai?.count||0), `Follow-up: ${p.ai?.followups||0}`, 'linear-gradient(135deg,var(--cyan-d),var(--cyan))'],
          ['fa-calendar-day','Số ngày hoạt động', (summary.days_used||0), `${summary.sessions||0} phiên`, 'linear-gradient(135deg,var(--green),#34D399)'],
        ].map(([ico,lbl,val,sub,bg]) => `
          <div class="ccu-kpi">
            <div class="ccu-kpi-ico" style="background:${bg}"><i class="fa-solid ${ico}"></i></div>
            <div>
              <div class="ccu-kpi-lbl">${esc(lbl)}</div>
              <div class="ccu-kpi-val">${esc(String(val))}</div>
              <div class="ccu-kpi-sub">${esc(sub)}</div>
            </div>
          </div>`).join('')}
      </div>

      <div class="ccp-grid">
        <div class="ccp-card ccp-c-8">
          <h4><i class="fa-solid fa-chart-line"></i> Hoạt động 30 ngày qua</h4>
          <div class="ccp-canvas-lg"><canvas id="ccp-daily"></canvas></div>
        </div>
        <div class="ccp-card ccp-c-4">
          <h4><i class="fa-solid fa-chart-pie"></i> Phân bổ hoạt động</h4>
          <div class="ccp-canvas"><canvas id="ccp-dist"></canvas></div>
        </div>

        <div class="ccp-card ccp-c-6">
          <h4><i class="fa-solid fa-flask"></i> Lab 3D</h4>
          <div class="ccp-canvas"><canvas id="ccp-lab"></canvas></div>
          <ul class="ccp-listcard" style="list-style:none; margin-top:14px;">
            ${(p.lab?.error_steps || []).slice(0, 5).map(([step, n]) => `
              <li><b><i class="fa-solid fa-triangle-exclamation" style="color:var(--red); margin-right:6px"></i>${esc(step)}</b><span>${n} lần sai</span></li>
            `).join('') || '<li><span>Chưa ghi nhận bước sai nào.</span></li>'}
          </ul>
        </div>

        <div class="ccp-card ccp-c-6">
          <h4><i class="fa-solid fa-book"></i> Bài học nổi bật</h4>
          <ul class="ccp-listcard" style="list-style:none;">
            ${(p.lesson?.top || []).slice(0, 8).map(([t, n]) => `
              <li><b>${esc(t)}</b><span>${n} lần</span></li>`).join('') || '<li><span>Chưa hoàn thành bài học nào.</span></li>'}
          </ul>
          <div class="ccp-mini" style="margin-top:14px;">
            <div class="v">${p.lesson?.completion_rate || 0}%</div>
            <div class="u">tỉ lệ hoàn thành trên số bài đã bắt đầu</div>
          </div>
        </div>

        <div class="ccp-card ccp-c-6">
          <h4><i class="fa-solid fa-robot"></i> AI Assistant</h4>
          <ul class="ccp-listcard" style="list-style:none;">
            ${(p.ai?.topics || []).slice(0, 8).map(([t, n]) => `
              <li><b>${esc(t)}</b><span>${n} lần</span></li>`).join('') || '<li><span>Chưa dùng AI.</span></li>'}
          </ul>
        </div>

        <div class="ccp-card ccp-c-6">
          <h4><i class="fa-solid fa-fire"></i> Heatmap hoạt động (giờ × ngày trong tuần)</h4>
          <div id="ccp-heatmap"></div>
        </div>

        <div class="ccp-card ccp-c-12">
          <h4><i class="fa-solid fa-timeline"></i> Nhật ký hoạt động</h4>
          <div class="ccp-t-scroll">
            <div class="ccp-timeline">
              ${(p.timeline || []).map(t => `
                <div class="ccp-t-item c-${esc(t.color||'text-m')}">
                  <div class="ccp-t-lbl"><i class="fa-solid ${esc(t.icon)}" style="margin-right:6px"></i>${esc(t.label)}</div>
                  <div class="ccp-t-time">${fmtTime(t.ts)}${t.ip?` · ${esc(t.ip)}`:''}</div>
                  ${Object.keys(t.extra||{}).length ? `<div class="ccp-t-extra">${
                    Object.entries(t.extra).slice(0,4).map(([k,v]) =>
                      `<code>${esc(k)}</code>: ${esc(typeof v==='object'?JSON.stringify(v).slice(0,80):String(v).slice(0,80))}`
                    ).join(' · ')
                  }</div>` : ''}
                </div>`).join('') || '<div class="ccu-empty" style="padding:30px"><i class="fa-solid fa-inbox"></i><div>Chưa có hoạt động nào.</div></div>'}
            </div>
          </div>
        </div>
      </div>
    `;

    $('#ccp-edit').onclick = async () => { await openUserForm(u); CC_Profile.render(uid); };
    $('#ccp-lock').onclick = async () => {
      await setUserLock(uid, !u.locked);
      u.locked = !u.locked;
      toast(u.locked ? 'Đã khóa tài khoản.' : 'Đã mở khóa.');
      CC_Profile.render(uid);
    };
    $('#ccp-reset').onclick = async () => {
      if (!window._fbAuth || !window._fbFns?.sendPasswordResetEmail) {
        toast('Cần import sendPasswordResetEmail (xem hướng dẫn).', 'warn'); return;
      }
      try {
        await window._fbFns.sendPasswordResetEmail(window._fbAuth, u.email);
        toast('Đã gửi email reset mật khẩu.');
      } catch (e) { toast(e.message, 'err'); }
    };

    // Charts
    if (window.Chart && p.daily) drawCharts(p);
    drawHeatmap(p.heatmap || []);
  }

  function drawCharts(p) {
    const grid   = getComputedStyle(document.body).getPropertyValue('--line').trim() || '#E4E9F1';
    const text   = getComputedStyle(document.body).getPropertyValue('--fg-soft').trim() || '#5B6477';
    const commonOpts = {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: text, font:{ family:'Be Vietnam Pro', size:11 } } } },
      scales: {
        x: { ticks: { color: text, font:{ size: 10 } }, grid: { color: 'transparent' } },
        y: { ticks: { color: text, font:{ size: 10 } }, grid: { color: grid + '55' }, beginAtZero: true },
      },
    };

    // Daily activity
    new Chart($('#ccp-daily'), {
      type: 'line',
      data: {
        labels: p.daily.map(d => d.date.slice(5)),
        datasets: [{
          label: 'Sự kiện / ngày',
          data: p.daily.map(d => d.count),
          fill: true,
          tension: 0.35,
          borderColor: '#06B6D4',
          backgroundColor: 'rgba(6,182,212,0.15)',
          pointRadius: 3, pointBackgroundColor: '#4F6BFF',
        }],
      },
      options: commonOpts,
    });

    // Distribution donut
    const dist = {
      'Bài học':   p.lesson?.completed || 0,
      'Quiz':      p.quiz?.done || 0,
      'Lab 3D':    p.lab?.opened || 0,
      'AI':        p.ai?.count || 0,
      'Xem trang': (p.summary?.page_views || 0),
    };
    new Chart($('#ccp-dist'), {
      type: 'doughnut',
      data: {
        labels: Object.keys(dist),
        datasets: [{
          data: Object.values(dist),
          backgroundColor: ['#4F6BFF', '#F59E0B', '#8B5CF6', '#06B6D4', '#94A3B8'],
          borderWidth: 0,
        }],
      },
      options: { ...commonOpts, cutout: '62%', scales: {} },
    });

    // Lab per experiment
    const labData = (p.lab?.by_name || []).slice(0, 8);
    new Chart($('#ccp-lab'), {
      type: 'bar',
      data: {
        labels: labData.map(([n]) => n.length > 22 ? n.slice(0, 20) + '…' : n),
        datasets: [{
          label: 'Số lần mở',
          data: labData.map(([, n]) => n),
          backgroundColor: 'rgba(139,92,246,0.75)',
          borderRadius: 6,
        }],
      },
      options: { ...commonOpts, indexAxis: 'y' },
    });
  }

  function drawHeatmap(m) {
    const dowLabels = ['T2','T3','T4','T5','T6','T7','CN'];
    const max = Math.max(1, ...m.flat());
    let html = `<div class="ccp-heatmap"><div></div>`;
    for (let h = 0; h < 24; h++) html += `<div class="h-hdr">${h}</div>`;
    for (let d = 0; d < 7; d++) {
      html += `<div class="h-lbl">${dowLabels[d]}</div>`;
      for (let h = 0; h < 24; h++) {
        const v = m[d]?.[h] || 0;
        const alpha = v ? (0.15 + 0.85 * (v / max)) : 0;
        html += `<div class="h-cell" title="${dowLabels[d]} ${h}h — ${v} sự kiện"
          style="${v?`background:rgba(6,182,212,${alpha.toFixed(2)})`:''}"></div>`;
      }
    }
    html += `</div>`;
    $('#ccp-heatmap').innerHTML = html;
  }

  window.CC_Profile = CC_Profile;

  // Cho phép navigateTo('profile', uid) truyền uid
  const _origNav = window.navigateTo;
  window.navigateTo = function (key, uid) {
    window._ccProfileUid = key === 'profile' ? (uid || window._ccProfileUid) : window._ccProfileUid;
    if (_origNav) _origNav(key);
  };
})();
