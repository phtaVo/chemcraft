/* ═══════════════════════════════════════════════════════════════════════
   CHEMCRAFT ADMIN — Module "Thanh toán" (Đơn hàng / Mã kích hoạt / Doanh thu)
   ---------------------------------------------------------------------
   Viết theo đúng khuôn của admin_lms_module.js: dùng chung X-Admin-Token,
   tự đăng ký vào window.CC_BILLING để admin.html gọi trong PAGES.

   Cách gắn vào admin.html:
     1. Thêm trước </body>, cạnh 2 dòng script module hiện có:
          <script src="admin_billing_module.js" defer></script>
     2. Thêm vào PAGES:
          PAGES['billing-orders']  = { title:'Đơn hàng',    sub:'Duyệt thanh toán chuyển khoản', render: () => window.CC_BILLING.renderOrders() };
          PAGES['billing-license'] = { title:'Mã kích hoạt', sub:'Gói trường học & tài trợ',      render: () => window.CC_BILLING.renderLicenses() };
          PAGES['billing-revenue'] = { title:'Doanh thu',    sub:'Thống kê nguồn thu',            render: () => window.CC_BILLING.renderRevenue() };
     3. Thêm 3 mục tương ứng vào sidebar nav.
   ═════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const esc = s => (s ?? '').toString()
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const vnd = n => new Intl.NumberFormat('vi-VN').format(n || 0) + 'đ';
  const fmtDT = ts => !ts ? '—'
    : new Date(ts * (ts < 1e12 ? 1000 : 1)).toLocaleString('vi-VN', { dateStyle: 'short', timeStyle: 'short' });

  const adminHeaders = () => ({
    'Content-Type': 'application/json',
    'X-Admin-Token': window.__adminToken || sessionStorage.getItem('cc_admin_token') || '',
  });

  const api = async (url, opts = {}) => {
    const r = await fetch('/api/billing' + url, {
      ...opts, headers: { ...(opts.headers || {}), ...adminHeaders() },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `API ${url} → ${r.status}`);
    return data;
  };

  const notify = msg => (window.toast ? window.toast(msg) : alert(msg));

  const STATUS = {
    pending:         ['#f9ab00', 'Chờ chuyển khoản'],
    awaiting_review: ['#e37400', 'CHỜ DUYỆT'],
    paid:            ['#1e8e3e', 'Đã thanh toán'],
    rejected:        ['#d93025', 'Từ chối'],
    expired:         ['#9aa0a6', 'Hết hạn'],
    cancelled:       ['#9aa0a6', 'Đã huỷ'],
  };
  const pill = st => {
    const [c, l] = STATUS[st] || ['#9aa0a6', st];
    return `<span style="background:${c}1a;color:${c};padding:3px 10px;border-radius:99px;font-size:.7rem;font-weight:700;">${esc(l)}</span>`;
  };

  // ═══════════════════════════════════════════════════════════════════
  // ĐƠN HÀNG
  // ═══════════════════════════════════════════════════════════════════
  async function renderOrders(filter = '') {
    const host = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    host.innerHTML = '<div style="padding:40px;text-align:center;color:#80868b;">Đang tải đơn hàng…</div>';
    let orders;
    try {
      ({ orders } = await api('/admin/orders' + (filter ? '?status=' + filter : '')));
    } catch (e) {
      host.innerHTML = `<div style="padding:30px;color:#d93025;">${esc(e.message)}</div>`;
      return;
    }

    // Đơn "chờ duyệt" (người mua đã bấm đã chuyển khoản) luôn nổi lên đầu —
    // đây là hàng đợi công việc thật sự của admin, không phải toàn bộ đơn.
    orders.sort((a, b) => (b.status === 'awaiting_review') - (a.status === 'awaiting_review'));

    const tabs = ['', 'awaiting_review', 'pending', 'paid', 'rejected'];
    const tabLabel = { '': 'Tất cả', awaiting_review: 'Chờ duyệt', pending: 'Chờ CK', paid: 'Đã thu', rejected: 'Từ chối' };

    host.innerHTML = `
      <div style="display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap;">
        ${tabs.map(t => `<button class="bill-tab" data-f="${t}"
          style="padding:8px 16px;border:none;border-radius:99px;cursor:pointer;font-size:.8125rem;font-weight:500;
          background:${t === filter ? '#1a73e8' : '#f1f3f4'};color:${t === filter ? '#fff' : '#5f6368'};">
          ${tabLabel[t]}</button>`).join('')}
        <button id="bill-refresh" style="margin-left:auto;padding:8px 16px;border:none;border-radius:8px;
          background:#f1f3f4;cursor:pointer;font-size:.8125rem;"><i class="fa-solid fa-rotate"></i> Làm mới</button>
      </div>
      <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;overflow:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:.8125rem;">
        <thead><tr style="color:#80868b;font-weight:500;">
          <th style="text-align:left;padding:12px;">Nội dung CK</th>
          <th style="text-align:left;padding:12px;">Người mua</th>
          <th style="text-align:left;padding:12px;">Gói</th>
          <th style="text-align:right;padding:12px;">Số tiền</th>
          <th style="text-align:left;padding:12px;">Tạo lúc</th>
          <th style="text-align:left;padding:12px;">Trạng thái</th>
          <th style="padding:12px;"></th>
        </tr></thead>
        <tbody>${orders.map(o => `
          <tr style="border-top:1px solid #e8eaed;">
            <td style="padding:12px;font-family:monospace;font-weight:700;">${esc(o.code)}</td>
            <td style="padding:12px;">${esc(o.buyerName || '—')}<br>
              <span style="color:#80868b;font-size:.75rem;">${esc(o.buyerEmail || '')}</span>
              ${o.orgName ? `<br><span style="color:#0b7f8e;font-size:.75rem;"><i class="fa-solid fa-school"></i> ${esc(o.orgName)}</span>` : ''}
              ${o.buyerPhone ? `<br><span style="color:#80868b;font-size:.75rem;">${esc(o.buyerPhone)}</span>` : ''}</td>
            <td style="padding:12px;">${esc(o.planName)}</td>
            <td style="padding:12px;text-align:right;font-weight:600;">${vnd(o.amount)}</td>
            <td style="padding:12px;color:#80868b;">${fmtDT(o.createdAt)}</td>
            <td style="padding:12px;">${pill(o.status)}</td>
            <td style="padding:12px;white-space:nowrap;">
              ${['pending', 'awaiting_review'].includes(o.status) ? `
                <button data-approve="${o.id}" style="background:#1e8e3e;color:#fff;border:none;padding:7px 13px;border-radius:6px;cursor:pointer;font-size:.75rem;font-weight:500;">
                  <i class="fa-solid fa-check"></i> Duyệt</button>
                <button data-reject="${o.id}" style="background:#fce8e6;color:#a50e0e;border:none;padding:7px 13px;border-radius:6px;cursor:pointer;font-size:.75rem;margin-left:5px;">
                  Từ chối</button>` : ''}
              ${o.licenseBatchId ? `<button data-batch="${o.licenseBatchId}" style="background:#e8f0fe;color:#174ea6;border:none;padding:7px 13px;border-radius:6px;cursor:pointer;font-size:.75rem;">Xem mã</button>` : ''}
            </td>
          </tr>`).join('') || '<tr><td colspan="7" style="padding:40px;text-align:center;color:#80868b;">Chưa có đơn hàng nào.</td></tr>'}
        </tbody>
      </table></div>`;

    host.querySelectorAll('.bill-tab').forEach(b => b.onclick = () => renderOrders(b.dataset.f));
    document.getElementById('bill-refresh').onclick = () => renderOrders(filter);

    host.querySelectorAll('[data-approve]').forEach(b => b.onclick = async () => {
      // Xác nhận hai bước có chủ ý: duyệt nhầm = tặng Premium miễn phí, và
      // không có thao tác "hoàn tác" nào lấy lại được thời hạn đã cấp.
      if (!confirm('Xác nhận ĐÃ NHẬN ĐỦ TIỀN cho đơn này?\n\nHãy đối chiếu nội dung chuyển khoản trong app ngân hàng trước khi duyệt. Premium sẽ được kích hoạt ngay.')) return;
      b.disabled = true;
      try {
        await api(`/admin/orders/${b.dataset.approve}/approve`, { method: 'POST', body: { note: '' } });
        notify('Đã duyệt đơn và kích hoạt Premium.');
        renderOrders(filter);
      } catch (e) { notify(e.message); b.disabled = false; }
    });

    host.querySelectorAll('[data-reject]').forEach(b => b.onclick = async () => {
      const note = prompt('Lý do từ chối (người mua sẽ thấy):', 'Không tìm thấy giao dịch chuyển khoản tương ứng.');
      if (note === null) return;
      try {
        await api(`/admin/orders/${b.dataset.reject}/reject`, { method: 'POST', body: { note } });
        notify('Đã từ chối đơn.');
        renderOrders(filter);
      } catch (e) { notify(e.message); }
    });

    host.querySelectorAll('[data-batch]').forEach(b => b.onclick = () => showBatchKeys(b.dataset.batch));
  }

  // ═══════════════════════════════════════════════════════════════════
  // MÃ KÍCH HOẠT
  // ═══════════════════════════════════════════════════════════════════
  async function renderLicenses() {
    const host = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    host.innerHTML = '<div style="padding:40px;text-align:center;color:#80868b;">Đang tải…</div>';
    let batches;
    try { ({ batches } = await api('/admin/license-batches')); }
    catch (e) { host.innerHTML = `<div style="padding:30px;color:#d93025;">${esc(e.message)}</div>`; return; }

    host.innerHTML = `
      <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:20px;margin-bottom:20px;">
        <h3 style="font-size:1rem;font-weight:500;margin-bottom:6px;">Phát hành lô mã mới</h3>
        <p style="font-size:.8125rem;color:#5f6368;line-height:1.6;margin-bottom:14px;">
          Dùng cho trường chuyển khoản theo hợp đồng (không đặt qua web), đợt tài trợ CSR của doanh nghiệp,
          hoặc triển khai thí điểm miễn phí tại trường THPT.</p>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;">
          <input id="b-label" placeholder="Tên trường / đợt phát hành" style="padding:10px;border:1px solid #dadce0;border-radius:8px;font-size:.8125rem;">
          <input id="b-qty" type="number" value="50" min="1" max="2000" placeholder="Số lượng mã" style="padding:10px;border:1px solid #dadce0;border-radius:8px;font-size:.8125rem;">
          <input id="b-days" type="number" value="365" min="1" placeholder="Số ngày hiệu lực" style="padding:10px;border:1px solid #dadce0;border-radius:8px;font-size:.8125rem;">
          <select id="b-kind" style="padding:10px;border:1px solid #dadce0;border-radius:8px;font-size:.8125rem;">
            <option value="student_premium">Premium học sinh</option>
            <option value="teacher_edu">ChemCraft for Edu (giáo viên)</option>
          </select>
          <input id="b-sponsor" placeholder="Nhà tài trợ (nếu có)" style="padding:10px;border:1px solid #dadce0;border-radius:8px;font-size:.8125rem;">
          <button id="b-create" style="background:#1a73e8;color:#fff;border:none;border-radius:8px;padding:10px;cursor:pointer;font-size:.8125rem;font-weight:500;">
            <i class="fa-solid fa-plus"></i> Phát hành</button>
        </div>
      </div>

      <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;overflow:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:.8125rem;">
        <thead><tr style="color:#80868b;font-weight:500;">
          <th style="text-align:left;padding:12px;">Lô</th><th style="text-align:left;padding:12px;">Loại</th>
          <th style="text-align:right;padding:12px;">Đã dùng / Tổng</th>
          <th style="text-align:left;padding:12px;">Hiệu lực</th>
          <th style="text-align:left;padding:12px;">Phát hành</th><th style="padding:12px;"></th>
        </tr></thead>
        <tbody>${batches.map(b => `
          <tr style="border-top:1px solid #e8eaed;">
            <td style="padding:12px;font-weight:500;">${esc(b.label || '(không tên)')}
              ${b.sponsor ? `<br><span style="color:#0b7f8e;font-size:.75rem;">Tài trợ: ${esc(b.sponsor)}</span>` : ''}</td>
            <td style="padding:12px;">${b.grantKind === 'teacher_edu' ? 'Giáo viên (Edu)' : 'Học sinh'}</td>
            <td style="padding:12px;text-align:right;font-weight:600;">${b.usedCount || 0} / ${b.quantity}</td>
            <td style="padding:12px;">${b.days} ngày</td>
            <td style="padding:12px;color:#80868b;">${fmtDT(b.createdAt)}</td>
            <td style="padding:12px;white-space:nowrap;">
              <button data-keys="${b.id}" style="background:#e8f0fe;color:#174ea6;border:none;padding:7px 13px;border-radius:6px;cursor:pointer;font-size:.75rem;">Xem mã</button>
              <a href="/api/billing/admin/license-batches/${b.id}/keys?format=csv" data-csv="${b.id}"
                 style="background:#f1f3f4;color:#5f6368;padding:7px 13px;border-radius:6px;font-size:.75rem;text-decoration:none;margin-left:5px;">CSV</a>
            </td>
          </tr>`).join('') || '<tr><td colspan="6" style="padding:40px;text-align:center;color:#80868b;">Chưa phát hành lô mã nào.</td></tr>'}
        </tbody></table></div>
      <div id="keys-panel"></div>`;

    document.getElementById('b-create').onclick = async () => {
      const qty = parseInt(document.getElementById('b-qty').value, 10) || 0;
      const label = document.getElementById('b-label').value.trim();
      if (!label) { notify('Vui lòng nhập tên trường / đợt phát hành.'); return; }
      if (!confirm(`Phát hành ${qty} mã kích hoạt cho "${label}"?`)) return;
      try {
        const batch = await api('/admin/license-batches', {
          method: 'POST',
          body: {
            label, quantity: qty,
            days: parseInt(document.getElementById('b-days').value, 10) || 365,
            grantKind: document.getElementById('b-kind').value,
            sponsor: document.getElementById('b-sponsor').value.trim(),
            planCode: document.getElementById('b-kind').value === 'teacher_edu' ? 'edu_teacher_1y' : 'school_class_1y',
          },
        });
        notify(`Đã phát hành ${batch.quantity} mã.`);
        renderLicenses();
      } catch (e) { notify(e.message); }
    };

    host.querySelectorAll('[data-keys]').forEach(b => b.onclick = () => showBatchKeys(b.dataset.keys));

    // Link CSV phải đi kèm X-Admin-Token, mà thẻ <a> thường không gửi được
    // header — tải bằng fetch rồi tạo blob URL.
    host.querySelectorAll('[data-csv]').forEach(a => a.onclick = async ev => {
      ev.preventDefault();
      try {
        const r = await fetch(a.getAttribute('href'), { headers: adminHeaders() });
        if (!r.ok) throw new Error('Không tải được file CSV.');
        const url = URL.createObjectURL(await r.blob());
        const link = document.createElement('a');
        link.href = url; link.download = `chemcraft-license-${a.dataset.csv}.csv`;
        link.click(); URL.revokeObjectURL(url);
      } catch (e) { notify(e.message); }
    });
  }

  async function showBatchKeys(batchId) {
    const panel = document.getElementById('keys-panel')
      || document.querySelector('#page-root, #main-content, .page-content') || document.body;
    panel.innerHTML = '<div style="padding:24px;text-align:center;color:#80868b;">Đang tải mã…</div>';
    try {
      const { keys } = await api(`/admin/license-batches/${batchId}/keys`);
      panel.innerHTML = `
        <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:20px;margin-top:20px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
            <h3 style="font-size:1rem;font-weight:500;">Mã trong lô (${keys.length})</h3>
            <button id="copy-unused" style="background:#f1f3f4;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:.8125rem;">
              <i class="fa-regular fa-copy"></i> Chép mã chưa dùng</button>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px;max-height:420px;overflow:auto;">
            ${keys.map(k => `<div style="font-family:monospace;font-size:.8125rem;padding:8px 10px;border-radius:6px;
              background:${k.status === 'used' ? '#f1f3f4' : '#e6f4ea'};color:${k.status === 'used' ? '#9aa0a6' : '#0d652d'};
              text-decoration:${k.status === 'used' ? 'line-through' : 'none'};">${esc(k.code)}</div>`).join('')}
          </div>
        </div>`;
      document.getElementById('copy-unused').onclick = () => {
        const unused = keys.filter(k => k.status !== 'used').map(k => k.code).join('\n');
        navigator.clipboard.writeText(unused).then(() => notify('Đã chép mã chưa dùng vào clipboard.'));
      };
    } catch (e) { panel.innerHTML = `<div style="padding:24px;color:#d93025;">${esc(e.message)}</div>`; }
  }

  // ═══════════════════════════════════════════════════════════════════
  // DOANH THU
  // ═══════════════════════════════════════════════════════════════════
  async function renderRevenue() {
    const host = document.querySelector('#page-root, #main-content, .page-content') || document.body;
    host.innerHTML = '<div style="padding:40px;text-align:center;color:#80868b;">Đang tính…</div>';
    let s;
    try { s = await api('/admin/revenue'); }
    catch (e) { host.innerHTML = `<div style="padding:30px;color:#d93025;">${esc(e.message)}</div>`; return; }

    const card = (label, value, color, sub) => `
      <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:20px;">
        <div style="font-size:.75rem;color:#80868b;font-weight:500;text-transform:uppercase;letter-spacing:.04em;">${label}</div>
        <div style="font-size:1.75rem;font-weight:500;color:${color};margin-top:8px;letter-spacing:-.02em;">${value}</div>
        ${sub ? `<div style="font-size:.75rem;color:#80868b;margin-top:4px;">${sub}</div>` : ''}
      </div>`;

    host.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:22px;">
        ${card('Tổng doanh thu', vnd(s.totalRevenue), '#1e8e3e', `${s.paidCount} đơn đã thanh toán`)}
        ${card('30 ngày gần nhất', vnd(s.revenue30d), '#1a73e8', '')}
        ${card('Giá trị đơn trung bình', vnd(s.arpu), '#0b7f8e', '')}
        ${card('Đang chờ xử lý', s.awaitingReviewCount, s.awaitingReviewCount ? '#e37400' : '#9aa0a6',
               `${s.pendingCount} đơn chưa chuyển khoản`)}
      </div>
      <div style="background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:20px;">
        <h3 style="font-size:1rem;font-weight:500;margin-bottom:14px;">Doanh thu theo gói</h3>
        <table style="width:100%;border-collapse:collapse;font-size:.8125rem;">
          <thead><tr style="color:#80868b;font-weight:500;">
            <th style="text-align:left;padding:10px;">Gói</th>
            <th style="text-align:right;padding:10px;">Số đơn</th>
            <th style="text-align:right;padding:10px;">Doanh thu</th>
            <th style="text-align:right;padding:10px;">Tỷ trọng</th>
          </tr></thead>
          <tbody>${s.byPlan.map(p => `
            <tr style="border-top:1px solid #e8eaed;">
              <td style="padding:11px;">${esc(p.name || p.planCode)}</td>
              <td style="padding:11px;text-align:right;">${p.count}</td>
              <td style="padding:11px;text-align:right;font-weight:600;">${vnd(p.revenue)}</td>
              <td style="padding:11px;text-align:right;color:#80868b;">
                ${s.totalRevenue ? Math.round(p.revenue / s.totalRevenue * 100) : 0}%</td>
            </tr>`).join('') || '<tr><td colspan="4" style="padding:36px;text-align:center;color:#80868b;">Chưa có doanh thu.</td></tr>'}
          </tbody></table>
      </div>`;
  }

  window.CC_BILLING = { renderOrders, renderLicenses, renderRevenue };
})();
