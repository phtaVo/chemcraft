"""
billing_db.py — Tầng dữ liệu cho cơ chế TRẢ PHÍ của ChemCraft.

Thiết kế theo đúng mô hình doanh thu trong thuyết minh dự án:
  1. Gói cá nhân Premium (B2C)  — học sinh/giáo viên tự mua, chuyển khoản
     VietQR, admin duyệt tay trong trang quản trị.
  2. Gói trường học (B2B2C)     — nhà trường ký hợp đồng/chuyển khoản
     offline, admin phát hành MỘT LÔ MÃ KÍCH HOẠT (license key) rồi gửi cho
     tổ Hoá; học sinh/giáo viên nhập mã để tự kích hoạt Premium.
  3. Tài trợ CSR                — dùng chung cơ chế lô mã kích hoạt, chỉ khác
     `sponsor` được ghi nhận để tri ân.

VÌ SAO CHỌN "CHUYỂN KHOẢN + DUYỆT TAY" THAY VÌ CỔNG THANH TOÁN:
  MoMo/VNPay/ZaloPay/PayOS đều yêu cầu pháp nhân (hộ kinh doanh hoặc công ty)
  mới mở được merchant. Ở giai đoạn hiện tại, VietQR vào tài khoản cá nhân +
  admin đối chiếu biến động số dư là cách hợp lệ, không phát sinh chi phí và
  triển khai được ngay. Khi dự án có pháp nhân, chỉ cần thêm một hàm
  `auto_confirm_from_webhook()` gọi vào `approve_order()` là chuyển sang tự
  động hoàn toàn — toàn bộ phần còn lại của hệ thống KHÔNG phải sửa.

KHÔNG dùng SQLite: giống lms_db.py, mọi thứ nằm trên Firestore vì Render free
tier xoá sạch đĩa mỗi lần redeploy — mất dữ liệu đơn hàng là mất tiền thật.

Collections mới:
  orders          — đơn hàng (chờ duyệt / đã thanh toán / từ chối / hết hạn)
  license_keys    — mã kích hoạt, mỗi mã là 1 document
  license_batches — lô mã (để admin quản lý theo trường/đợt phát hành)
"""
import os
import random
import string
import time

from firebase_admin import firestore as fb_firestore

import firestore_db as fsdb
import lms_db

# ══════════════════════════════════════════════════════════════════════
# CẤU HÌNH TÀI KHOẢN NHẬN TIỀN
# ══════════════════════════════════════════════════════════════════════
# Đặt trong biến môi trường trên Render (Settings → Environment), KHÔNG
# hardcode số tài khoản vào repo public.
#   BANK_BIN   : mã BIN ngân hàng theo chuẩn NAPAS (VD Vietcombank 970436,
#                MB Bank 970422, Techcombank 970407, BIDV 970418, ACB 970416)
#   BANK_ACCOUNT_NO   : số tài khoản nhận tiền
#   BANK_ACCOUNT_NAME : tên chủ tài khoản (in hoa, không dấu)
BANK_BIN          = os.getenv('BANK_BIN', '970422')
BANK_ACCOUNT_NO   = os.getenv('BANK_ACCOUNT_NO', '')
BANK_ACCOUNT_NAME = os.getenv('BANK_ACCOUNT_NAME', 'VO DUC PHAT')
BANK_NAME         = os.getenv('BANK_NAME', 'MB Bank')

ORDER_TTL_SEC = 48 * 3600      # đơn chưa chuyển khoản sau 48h coi như huỷ
LICENSE_TTL_SEC = 365 * 86400  # mã kích hoạt chưa dùng hết hạn sau 1 năm


# ══════════════════════════════════════════════════════════════════════
# DANH MỤC GÓI (PLAN CATALOG)
# ══════════════════════════════════════════════════════════════════════
# `kind` quyết định điều gì xảy ra khi đơn được duyệt:
#   student_premium — cấp Premium cho chính người mua (lms_db.grant_student_premium)
#   teacher_edu     — cấp gói "ChemCraft for edu" cho giáo viên, kéo theo cả lớp
#   school_license  — KHÔNG cấp trực tiếp cho ai, mà sinh ra 1 lô mã kích hoạt
#
# Giá để ở đây (không nằm trong Firestore) để admin không sửa nhầm giá trong
# lúc đang có đơn treo — đổi giá là một lần deploy, có lịch sử git rõ ràng.
PLANS = {
    'premium_1m': {
        'name': 'Premium học sinh — 1 tháng',
        'kind': 'student_premium',
        'amount': 29000,
        'days': 30,
        'audience': 'student',
        'features': [
            'Không giới hạn lượt giải bài AI mỗi ngày',
            'Mở khoá toàn bộ phản ứng Premium trong phòng Lab 3D',
            'Chat AI không giới hạn ngay trong phòng thí nghiệm',
            'Không giới hạn lượt nộp bài tập mỗi ngày',
        ],
    },
    'premium_6m': {
        'name': 'Premium học sinh — 6 tháng (1 học kỳ)',
        'kind': 'student_premium',
        'amount': 149000,
        'days': 183,
        'audience': 'student',
        'badge': 'Tiết kiệm 15%',
        'features': [
            'Toàn bộ quyền lợi gói 1 tháng',
            'Giá trung bình chỉ ~24.800đ/tháng',
            'Đủ dùng trọn một học kỳ ôn thi',
        ],
    },
    'premium_12m': {
        'name': 'Premium học sinh — 12 tháng (1 năm học)',
        'kind': 'student_premium',
        'amount': 249000,
        'days': 365,
        'audience': 'student',
        'badge': 'Phổ biến nhất',
        'features': [
            'Toàn bộ quyền lợi gói 6 tháng',
            'Giá trung bình chỉ ~20.750đ/tháng',
            'Trọn năm học, không lo gián đoạn giữa kỳ',
        ],
    },
    'edu_teacher_1y': {
        'name': 'ChemCraft for Edu — Giáo viên, 1 năm học',
        'kind': 'teacher_edu',
        'amount': 490000,
        'days': 365,
        'audience': 'teacher',
        'features': [
            'Toàn bộ quyền Premium cho tài khoản giáo viên',
            'Học sinh trong các lớp do thầy/cô chủ nhiệm được Premium theo lớp',
            'Không giới hạn lớp học, bài tập, bài kiểm tra, livestream',
            'Báo cáo tiến độ và năng lực thực hành của từng học sinh',
        ],
    },
    'school_class_1y': {
        'name': 'Gói Lớp học — 50 mã kích hoạt, 1 năm học',
        'kind': 'school_license',
        'amount': 1500000,
        'days': 365,
        'quantity': 50,
        'grant_kind': 'student_premium',
        'audience': 'school',
        'features': [
            '50 mã kích hoạt Premium cho học sinh, dùng trọn năm học',
            'Nhà trường tự phân phối mã theo lớp',
            'Trung bình 30.000đ/học sinh/năm',
        ],
    },
    'school_full_1y': {
        'name': 'Gói Toàn trường — 500 mã kích hoạt, 1 năm học',
        'kind': 'school_license',
        'amount': 9000000,
        'days': 365,
        'quantity': 500,
        'grant_kind': 'student_premium',
        'audience': 'school',
        'badge': 'Tiết kiệm 40%',
        'features': [
            '500 mã kích hoạt Premium cho học sinh toàn trường',
            '10 tài khoản giáo viên ChemCraft for Edu (admin cấp riêng)',
            'Trung bình 18.000đ/học sinh/năm',
            'Hỗ trợ tập huấn sử dụng cho tổ bộ môn Hoá',
        ],
    },
}


def list_plans(audience: str = '') -> list[dict]:
    """Danh mục gói công khai cho trang nâng cấp. Có kèm mã gói để frontend
    gửi lại khi tạo đơn — giá KHÔNG bao giờ nhận từ frontend (xem create_order)."""
    out = []
    for code, p in PLANS.items():
        if audience and p.get('audience') != audience:
            continue
        out.append({'code': code, **{k: v for k, v in p.items() if k != 'kind'}, 'kind': p['kind']})
    return sorted(out, key=lambda x: x['amount'])


# ══════════════════════════════════════════════════════════════════════
# TIỆN ÍCH
# ══════════════════════════════════════════════════════════════════════

def _now() -> float:
    return time.time()


_CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'  # bỏ 0/O/1/I dễ đọc nhầm khi chép tay


def _rand(n: int) -> str:
    return ''.join(random.choices(_CODE_ALPHABET, k=n))


def _gen_order_code() -> str:
    """Mã đơn hàng = NỘI DUNG CHUYỂN KHOẢN người dùng phải ghi. Phải ngắn,
    không dấu, không khoảng trắng để app ngân hàng không cắt mất, và đủ ngẫu
    nhiên để hai học sinh không bao giờ trùng nội dung chuyển khoản."""
    return 'CC' + _rand(8)


def _gen_license_code() -> str:
    return f'CC-{_rand(4)}-{_rand(4)}-{_rand(4)}'


def vietqr_url(amount: int, add_info: str) -> str:
    """Ảnh VietQR động do vietqr.io sinh — quét bằng bất kỳ app ngân hàng nào,
    số tiền và nội dung chuyển khoản đã điền sẵn nên người dùng không gõ sai
    nội dung (nguyên nhân số 1 khiến đơn không đối soát được)."""
    if not BANK_ACCOUNT_NO:
        return ''
    from urllib.parse import quote
    return (
        f'https://img.vietqr.io/image/{BANK_BIN}-{BANK_ACCOUNT_NO}-compact2.png'
        f'?amount={int(amount)}&addInfo={quote(add_info)}&accountName={quote(BANK_ACCOUNT_NAME)}'
    )


def payment_info(amount: int, add_info: str) -> dict:
    return {
        'bankName': BANK_NAME,
        'accountNo': BANK_ACCOUNT_NO,
        'accountName': BANK_ACCOUNT_NAME,
        'amount': amount,
        'transferContent': add_info,
        'qrUrl': vietqr_url(amount, add_info),
        'configured': bool(BANK_ACCOUNT_NO),
    }


# ══════════════════════════════════════════════════════════════════════
# ĐƠN HÀNG
# ══════════════════════════════════════════════════════════════════════

def create_order(uid: str, plan_code: str, buyer: dict | None = None) -> dict:
    """Tạo đơn ở trạng thái `pending`. Giá LẤY TỪ PLANS Ở SERVER, tuyệt đối
    không lấy từ body request — nếu tin frontend thì ai cũng có thể sửa
    devtools để mua gói 12 tháng với giá 1.000đ."""
    plan = PLANS.get(plan_code)
    if not plan:
        raise ValueError('Gói không tồn tại.')
    buyer = buyer or {}

    # Không cho một người treo hàng chục đơn chờ duyệt cùng lúc (rác cho admin).
    pending = list_orders(uid=uid, status='pending')
    if len(pending) >= 3:
        raise ValueError('Bạn đang có quá nhiều đơn chờ thanh toán. '
                         'Vui lòng hoàn tất hoặc huỷ bớt trước khi tạo đơn mới.')

    code = _gen_order_code()
    ts = _now()
    doc = {
        'code': code,
        'uid': uid,
        'planCode': plan_code,
        'planName': plan['name'],
        'kind': plan['kind'],
        'amount': int(plan['amount']),
        'days': int(plan.get('days', 30)),
        'quantity': int(plan.get('quantity', 1)),
        'status': 'pending',
        'buyerName': (buyer.get('name') or '')[:120],
        'buyerEmail': (buyer.get('email') or '')[:160],
        'buyerPhone': (buyer.get('phone') or '')[:40],
        'orgName': (buyer.get('orgName') or '')[:200],   # tên trường, cho gói B2B
        'note': (buyer.get('note') or '')[:500],
        'createdAt': ts,
        'expiresAt': ts + ORDER_TTL_SEC,
        'paidAt': None,
        'reviewedBy': None,
        'reviewNote': None,
        'licenseBatchId': None,
    }
    ref = fsdb.collection('orders').document()
    ref.set(doc)
    doc['id'] = ref.id
    doc['payment'] = payment_info(doc['amount'], code)
    return doc


def get_order(order_id: str) -> dict | None:
    snap = fsdb.collection('orders').document(order_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d['id'] = order_id
    return d


def get_order_by_code(code: str) -> dict | None:
    q = fsdb.collection('orders').where('code', '==', (code or '').strip().upper()).limit(1)
    for snap in q.stream():
        d = snap.to_dict()
        d['id'] = snap.id
        return d
    return None


def list_orders(uid: str = '', status: str = '', limit: int = 200) -> list[dict]:
    col = fsdb.collection('orders')
    q = col
    if uid:
        q = q.where('uid', '==', uid)
    if status:
        q = q.where('status', '==', status)
    out = []
    for snap in q.limit(limit).stream():
        d = snap.to_dict()
        d['id'] = snap.id
        out.append(d)
    # Sắp xếp ở Python thay vì order_by trên Firestore: tránh bắt buộc phải
    # tạo composite index cho từng tổ hợp where+order_by (Firestore yêu cầu),
    # vốn rất dễ làm endpoint chết ở production với lỗi FAILED_PRECONDITION.
    out.sort(key=lambda x: x.get('createdAt', 0), reverse=True)
    return out


def expire_stale_orders() -> int:
    """Chuyển các đơn quá hạn chưa chuyển khoản sang `expired`. Gọi lười
    (lazy) mỗi lần admin mở danh sách đơn — không cần cron job riêng, đúng
    kiểu _expire_student_plan_if_needed() trong lms_db.py."""
    n = 0
    now = _now()
    for o in list_orders(status='pending'):
        if o.get('expiresAt') and now > o['expiresAt']:
            fsdb.collection('orders').document(o['id']).set(
                {'status': 'expired', 'updatedAt': now}, merge=True)
            n += 1
    return n


def cancel_order(order_id: str, uid: str) -> bool:
    """Người mua tự huỷ đơn chưa thanh toán của chính mình."""
    o = get_order(order_id)
    if not o or o.get('uid') != uid or o.get('status') != 'pending':
        return False
    fsdb.collection('orders').document(order_id).set(
        {'status': 'cancelled', 'updatedAt': _now()}, merge=True)
    return True


def mark_transferred(order_id: str, uid: str) -> bool:
    """Người mua bấm 'Tôi đã chuyển khoản' — chỉ đổi nhãn để đơn nổi lên đầu
    hàng đợi của admin, KHÔNG cấp Premium. Việc cấp quyền chỉ xảy ra ở
    approve_order() sau khi admin đối chiếu biến động số dư thật."""
    o = get_order(order_id)
    if not o or o.get('uid') != uid or o.get('status') != 'pending':
        return False
    fsdb.collection('orders').document(order_id).set(
        {'status': 'awaiting_review', 'transferredAt': _now(), 'updatedAt': _now()}, merge=True)
    return True


def approve_order(order_id: str, reviewer: str = 'admin', note: str = '') -> dict:
    """Admin xác nhận đã nhận đủ tiền → kích hoạt quyền lợi tương ứng.

    Idempotent: gọi hai lần trên cùng một đơn không cộng dồn thời hạn, vì
    admin rất dễ bấm duyệt hai lần khi mạng chậm.
    """
    o = get_order(order_id)
    if not o:
        raise ValueError('Không tìm thấy đơn hàng.')
    if o.get('status') == 'paid':
        return o

    ts = _now()
    patch = {'status': 'paid', 'paidAt': ts, 'reviewedBy': reviewer,
             'reviewNote': note[:500], 'updatedAt': ts}

    kind = o.get('kind')
    days = int(o.get('days', 30))

    if kind == 'student_premium':
        lms_db.grant_student_premium(o['uid'], f'order:{o["code"]}', days=days)

    elif kind == 'teacher_edu':
        lms_db.set_teacher_plan(o['uid'], True)
        # set_teacher_plan() không có hạn dùng; ghi thêm mốc hết hạn để admin
        # biết khi nào cần thu hồi/gia hạn (xem list_expiring_edu_plans).
        fsdb.collection('users').document(o['uid']).set(
            {'eduPlanExpiresAt': ts + days * 86400, 'updatedAt': ts}, merge=True)

    elif kind == 'school_license':
        batch = create_license_batch(
            plan_code=o['planCode'], quantity=int(o.get('quantity', 1)),
            days=days, grant_kind=PLANS[o['planCode']].get('grant_kind', 'student_premium'),
            label=o.get('orgName') or o.get('buyerName') or 'Gói trường học',
            order_id=order_id, created_by=reviewer,
        )
        patch['licenseBatchId'] = batch['id']

    fsdb.collection('orders').document(order_id).set(patch, merge=True)
    o.update(patch)
    return o


def reject_order(order_id: str, reviewer: str = 'admin', note: str = '') -> bool:
    o = get_order(order_id)
    if not o or o.get('status') == 'paid':
        return False
    fsdb.collection('orders').document(order_id).set({
        'status': 'rejected', 'reviewedBy': reviewer,
        'reviewNote': note[:500], 'updatedAt': _now(),
    }, merge=True)
    return True


# ══════════════════════════════════════════════════════════════════════
# MÃ KÍCH HOẠT (gói trường học + tài trợ CSR)
# ══════════════════════════════════════════════════════════════════════

def create_license_batch(plan_code: str, quantity: int, days: int,
                         grant_kind: str = 'student_premium', label: str = '',
                         order_id: str | None = None, created_by: str = 'admin',
                         sponsor: str = '') -> dict:
    """Sinh một lô mã kích hoạt. Dùng cho (a) đơn gói trường học đã thanh
    toán, (b) đợt tài trợ CSR do doanh nghiệp/quỹ khuyến học trao tặng,
    (c) đợt triển khai thí điểm miễn phí tại trường."""
    quantity = max(1, min(int(quantity), 2000))  # chặn lỗi gõ nhầm 50000 mã
    days = int(days)
    ts = _now()

    bref = fsdb.collection('license_batches').document()
    bref.set({
        'planCode': plan_code, 'label': label[:200], 'quantity': quantity,
        'days': days, 'grantKind': grant_kind, 'orderId': order_id,
        'sponsor': sponsor[:200], 'createdBy': created_by, 'createdAt': ts,
        'expiresAt': ts + LICENSE_TTL_SEC,
    })

    codes = []
    col = fsdb.collection('license_keys')
    batch = fsdb.init().batch()
    for i in range(quantity):
        code = _gen_license_code()
        codes.append(code)
        batch.set(col.document(code), {
            'batchId': bref.id, 'planCode': plan_code, 'grantKind': grant_kind,
            'days': days, 'status': 'unused', 'redeemedBy': None,
            'redeemedAt': None, 'createdAt': ts, 'expiresAt': ts + LICENSE_TTL_SEC,
        })
        # Firestore giới hạn 500 thao tác/batch — commit theo từng khối 400.
        if (i + 1) % 400 == 0:
            batch.commit()
            batch = fsdb.init().batch()
    batch.commit()

    return {'id': bref.id, 'label': label, 'quantity': quantity,
            'days': days, 'planCode': plan_code, 'codes': codes}


def list_license_batches(limit: int = 100) -> list[dict]:
    out = []
    for snap in fsdb.collection('license_batches').limit(limit).stream():
        d = snap.to_dict()
        d['id'] = snap.id
        d['usedCount'] = _count_keys(d['id'], 'used')
        out.append(d)
    out.sort(key=lambda x: x.get('createdAt', 0), reverse=True)
    return out


def _count_keys(batch_id: str, status: str) -> int:
    q = (fsdb.collection('license_keys')
         .where('batchId', '==', batch_id).where('status', '==', status))
    return fsdb._agg_count(q)


def list_license_keys(batch_id: str, limit: int = 2000) -> list[dict]:
    out = []
    q = fsdb.collection('license_keys').where('batchId', '==', batch_id).limit(limit)
    for snap in q.stream():
        d = snap.to_dict()
        d['code'] = snap.id
        out.append(d)
    out.sort(key=lambda x: x.get('code', ''))
    return out


def redeem_license(code: str, uid: str) -> dict:
    """Học sinh/giáo viên nhập mã để tự kích hoạt Premium.

    Dùng transaction của Firestore: nếu hai học sinh cùng nhập một mã trong
    cùng một khoảnh khắc (mã bị chụp màn hình chia sẻ trong nhóm lớp), chỉ
    đúng một người kích hoạt được — kiểm tra rồi mới ghi kiểu thông thường
    sẽ để lọt cả hai.
    """
    code = (code or '').strip().upper().replace(' ', '')
    if not code:
        raise ValueError('Vui lòng nhập mã kích hoạt.')

    db = fsdb.init()
    ref = fsdb.collection('license_keys').document(code)

    @fb_firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError('Mã kích hoạt không tồn tại. Vui lòng kiểm tra lại.')
        d = snap.to_dict()
        if d.get('status') == 'used':
            raise ValueError('Mã này đã được sử dụng.')
        if d.get('expiresAt') and _now() > d['expiresAt']:
            raise ValueError('Mã kích hoạt đã hết hạn.')
        transaction.update(ref, {
            'status': 'used', 'redeemedBy': uid, 'redeemedAt': _now(),
        })
        return d

    data = _txn(db.transaction())

    days = int(data.get('days', 365))
    if data.get('grantKind') == 'teacher_edu':
        lms_db.set_teacher_plan(uid, True)
        fsdb.collection('users').document(uid).set(
            {'eduPlanExpiresAt': _now() + days * 86400, 'updatedAt': _now()}, merge=True)
    else:
        lms_db.grant_student_premium(uid, f'license:{code}', days=days)

    return {'ok': True, 'days': days, 'planCode': data.get('planCode'),
            'grantKind': data.get('grantKind', 'student_premium')}


# ══════════════════════════════════════════════════════════════════════
# THỐNG KÊ DOANH THU (cho trang quản trị)
# ══════════════════════════════════════════════════════════════════════

def revenue_stats() -> dict:
    orders = list_orders(limit=1000)
    paid = [o for o in orders if o.get('status') == 'paid']
    now = _now()
    month_ago = now - 30 * 86400

    by_plan: dict[str, dict] = {}
    for o in paid:
        e = by_plan.setdefault(o.get('planCode', '?'),
                               {'name': o.get('planName', ''), 'count': 0, 'revenue': 0})
        e['count'] += 1
        e['revenue'] += int(o.get('amount', 0))

    return {
        'totalRevenue': sum(int(o.get('amount', 0)) for o in paid),
        'revenue30d': sum(int(o.get('amount', 0)) for o in paid
                          if (o.get('paidAt') or 0) > month_ago),
        'paidCount': len(paid),
        'pendingCount': len([o for o in orders if o.get('status') == 'pending']),
        'awaitingReviewCount': len([o for o in orders if o.get('status') == 'awaiting_review']),
        'byPlan': [{'planCode': k, **v} for k, v in
                   sorted(by_plan.items(), key=lambda x: -x[1]['revenue'])],
        'arpu': round(sum(int(o.get('amount', 0)) for o in paid) / len(paid)) if paid else 0,
    }
