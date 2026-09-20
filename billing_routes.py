"""
billing_routes.py — API cho cơ chế trả phí (/api/billing/*).

Gắn vào server.py giống hệt lms_routes:

    import billing_routes
    billing_routes.register(app)

Phân tầng xác thực giữ nguyên quy ước sẵn có của dự án:
  - Người mua (học sinh/giáo viên) → Firebase ID token qua lms_auth.require_auth
  - Trang quản trị (admin.html)    → header X-Admin-Token qua lms_auth.require_admin_token

NGUYÊN TẮC AN TOÀN TIỀN BẠC (đọc trước khi sửa file này):
  1. Giá tiền KHÔNG BAO GIỜ đọc từ request body — luôn tra trong billing_db.PLANS.
  2. Không endpoint nào do người dùng gọi được phép cấp Premium trực tiếp.
     Chỉ approve_order() (cần X-Admin-Token) và redeem_license() (tiêu huỷ một
     mã kích hoạt trong transaction) mới thay đổi quyền của user.
  3. uid luôn lấy từ token đã verify (request.lms_user['uid']), không lấy từ body.
"""
import logging
import traceback

from flask import Blueprint, request, jsonify

import billing_db
import lms_auth
import lms_db
import firestore_db as fsdb

bp = Blueprint('billing', __name__, url_prefix='/api/billing')
logger = logging.getLogger('billing_routes')


def register(app):
    app.register_blueprint(bp)


@bp.errorhandler(Exception)
def _handle(e):
    logger.error('Unhandled exception on %s %s: %s\n%s',
                 request.method, request.path, e, traceback.format_exc())
    return jsonify({'error': 'Lỗi hệ thống, vui lòng thử lại sau.'}), 500


# ══════════════════════════════════════════════════════════════════════
# CÔNG KHAI — bảng giá
# ══════════════════════════════════════════════════════════════════════

@bp.route('/plans', methods=['GET'])
def plans():
    """Không yêu cầu đăng nhập: trang giá phải xem được trước khi tạo tài
    khoản, nếu không sẽ mất phần lớn người quan tâm ngay ở bước đầu."""
    audience = (request.args.get('audience') or '').strip()
    return jsonify({'plans': billing_db.list_plans(audience)})


# ══════════════════════════════════════════════════════════════════════
# NGƯỜI MUA
# ══════════════════════════════════════════════════════════════════════

@bp.route('/orders', methods=['POST'])
@lms_auth.require_auth
def create_order():
    u = request.lms_user
    b = request.get_json(silent=True) or {}
    plan_code = (b.get('planCode') or '').strip()
    try:
        order = billing_db.create_order(u['uid'], plan_code, buyer={
            'name': b.get('name') or u.get('displayName', ''),
            'email': b.get('email') or u.get('email', ''),
            'phone': b.get('phone', ''),
            'orgName': b.get('orgName', ''),
            'note': b.get('note', ''),
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    fsdb.record_event('billing_order_created', {
        'orderCode': order['code'], 'planCode': plan_code, 'amount': order['amount'],
    }, user_id=u['uid'])
    return jsonify(order), 201


@bp.route('/orders/mine', methods=['GET'])
@lms_auth.require_auth
def my_orders():
    orders = billing_db.list_orders(uid=request.lms_user['uid'])
    for o in orders:
        if o.get('status') in ('pending', 'awaiting_review'):
            o['payment'] = billing_db.payment_info(o.get('amount', 0), o.get('code', ''))
    return jsonify({'orders': orders})


@bp.route('/orders/<order_id>', methods=['GET'])
@lms_auth.require_auth
def get_order(order_id):
    o = billing_db.get_order(order_id)
    # Không trả 404 chung chung cho đơn của người khác rồi mới kiểm tra quyền —
    # trả cùng một thông báo để không lộ việc mã đơn đó có tồn tại hay không.
    if not o or o.get('uid') != request.lms_user['uid']:
        return jsonify({'error': 'Không tìm thấy đơn hàng.'}), 404
    if o.get('status') in ('pending', 'awaiting_review'):
        o['payment'] = billing_db.payment_info(o.get('amount', 0), o.get('code', ''))
    return jsonify(o)


@bp.route('/orders/<order_id>/transferred', methods=['POST'])
@lms_auth.require_auth
def mark_transferred(order_id):
    ok = billing_db.mark_transferred(order_id, request.lms_user['uid'])
    if not ok:
        return jsonify({'error': 'Không thể cập nhật đơn hàng này.'}), 400
    return jsonify({'ok': True, 'status': 'awaiting_review'})


@bp.route('/orders/<order_id>', methods=['DELETE'])
@lms_auth.require_auth
def cancel_order(order_id):
    ok = billing_db.cancel_order(order_id, request.lms_user['uid'])
    if not ok:
        return jsonify({'error': 'Chỉ huỷ được đơn đang chờ thanh toán.'}), 400
    return jsonify({'ok': True})


@bp.route('/redeem', methods=['POST'])
@lms_auth.require_auth
def redeem():
    """Nhập mã kích hoạt từ gói trường học hoặc đợt tài trợ."""
    u = request.lms_user
    code = (request.get_json(silent=True) or {}).get('code', '')
    try:
        result = billing_db.redeem_license(code, u['uid'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    fsdb.record_event('billing_license_redeemed', {
        'planCode': result.get('planCode'), 'days': result.get('days'),
    }, user_id=u['uid'])
    return jsonify({**result, 'usage': lms_db.usage_snapshot(u['uid'])})


@bp.route('/my-subscription', methods=['GET'])
@lms_auth.require_auth
def my_subscription():
    """Gói hiện tại + hạn dùng, để trang nâng cấp biết nên hiện 'Nâng cấp' hay
    'Gia hạn' và còn bao nhiêu ngày."""
    u = lms_db.ensure_user_defaults(request.lms_user['uid'])
    return jsonify({
        'unlimited': lms_db.has_unlimited_access(u),
        'role': u.get('role'),
        'plan': u.get('plan'), 'planStatus': u.get('planStatus'),
        'studentPlanStatus': u.get('studentPlanStatus'),
        'studentPlanExpiresAt': u.get('studentPlanExpiresAt'),
        'educationPlanStatus': u.get('educationPlanStatus'),
        'eduPlanExpiresAt': u.get('eduPlanExpiresAt'),
        'usage': lms_db.usage_snapshot(u['uid']),
    })


# ══════════════════════════════════════════════════════════════════════
# QUẢN TRỊ
# ══════════════════════════════════════════════════════════════════════

@bp.route('/admin/orders', methods=['GET'])
@lms_auth.require_admin_token
def admin_orders():
    billing_db.expire_stale_orders()
    status = (request.args.get('status') or '').strip()
    orders = billing_db.list_orders(status=status, limit=500)
    # Gắn thêm tên/email người mua từ collection users để admin đối chiếu
    # với nội dung chuyển khoản mà không phải mở thêm tab quản lý người dùng.
    for o in orders:
        if not o.get('buyerEmail'):
            u = lms_db.get_user(o.get('uid', ''))
            if u:
                o['buyerEmail'] = u.get('email', '')
                o['buyerName'] = o.get('buyerName') or u.get('displayName', '')
    return jsonify({'orders': orders})


@bp.route('/admin/orders/<order_id>/approve', methods=['POST'])
@lms_auth.require_admin_token
def admin_approve(order_id):
    note = (request.get_json(silent=True) or {}).get('note', '')
    reviewer = request.admin_session.get('username', 'admin')
    try:
        o = billing_db.approve_order(order_id, reviewer=reviewer, note=note)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    fsdb.record_event('billing_order_approved', {
        'orderCode': o.get('code'), 'amount': o.get('amount'), 'by': reviewer,
    }, user_id=o.get('uid', ''))
    return jsonify(o)


@bp.route('/admin/orders/<order_id>/reject', methods=['POST'])
@lms_auth.require_admin_token
def admin_reject(order_id):
    note = (request.get_json(silent=True) or {}).get('note', '')
    reviewer = request.admin_session.get('username', 'admin')
    if not billing_db.reject_order(order_id, reviewer=reviewer, note=note):
        return jsonify({'error': 'Không thể từ chối đơn này (có thể đã duyệt).'}), 400
    return jsonify({'ok': True})


@bp.route('/admin/license-batches', methods=['GET'])
@lms_auth.require_admin_token
def admin_list_batches():
    return jsonify({'batches': billing_db.list_license_batches()})


@bp.route('/admin/license-batches', methods=['POST'])
@lms_auth.require_admin_token
def admin_create_batch():
    """Phát hành lô mã thủ công: dùng cho trường chuyển khoản theo hợp đồng
    (không tạo đơn trên web), đợt tài trợ CSR, hoặc triển khai thí điểm
    miễn phí tại 3-5 trường THPT."""
    b = request.get_json(silent=True) or {}
    try:
        batch = billing_db.create_license_batch(
            plan_code=(b.get('planCode') or 'school_class_1y'),
            quantity=int(b.get('quantity', 10)),
            days=int(b.get('days', 365)),
            grant_kind=(b.get('grantKind') or 'student_premium'),
            label=(b.get('label') or ''),
            sponsor=(b.get('sponsor') or ''),
            created_by=request.admin_session.get('username', 'admin'),
        )
    except (TypeError, ValueError) as e:
        return jsonify({'error': f'Dữ liệu không hợp lệ: {e}'}), 400
    return jsonify(batch), 201


@bp.route('/admin/license-batches/<batch_id>/keys', methods=['GET'])
@lms_auth.require_admin_token
def admin_batch_keys(batch_id):
    keys = billing_db.list_license_keys(batch_id)
    if (request.args.get('format') or '') == 'csv':
        lines = ['code,status,redeemedBy,redeemedAt']
        for k in keys:
            lines.append(f"{k['code']},{k.get('status','')},{k.get('redeemedBy') or ''},"
                         f"{k.get('redeemedAt') or ''}")
        from flask import Response
        return Response('\n'.join(lines), mimetype='text/csv', headers={
            'Content-Disposition': f'attachment; filename=chemcraft-license-{batch_id}.csv'})
    return jsonify({'keys': keys})


@bp.route('/admin/revenue', methods=['GET'])
@lms_auth.require_admin_token
def admin_revenue():
    return jsonify(billing_db.revenue_stats())
