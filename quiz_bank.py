"""
quiz_bank.py — Ngân hàng câu hỏi trắc nghiệm cho tab Quiz.
=============================================================
Trước đây 34 câu trắc nghiệm (+ 2 câu thực hành) nằm CỨNG trong biến
`quizData` ở lesson.html — admin không thể thêm/sửa/xóa, và học sinh nào
cũng làm đúng 1 bộ đề y hệt theo thứ tự y hệt.

Module này chuyển 34 câu trắc nghiệm đó vào bảng `quiz_questions` (SQLite,
xem database.py) và cung cấp:

  - seed_default_questions()      : chạy 1 lần lúc khởi động server, chỉ
                                     insert 34 câu gốc NẾU bảng đang trống,
                                     để không mất/không lặp dữ liệu cũ.
  - list_questions(include_inactive)
  - get_question(qid)
  - create_question(...)
  - update_question(qid, ...)
  - delete_question(qid)          : xóa hẳn khỏi ngân hàng
  - set_active(qid, active)       : ẩn/hiện mà không xóa
  - random_questions(count)       : rút ngẫu nhiên `count` câu đang active
                                     (dùng cho /api/quiz-questions)

2 câu thực hành (thao tác trực tiếp trên mini-lab ảo trong lesson.html)
KHÔNG đưa vào đây vì chúng gắn với DOM/JS đặc thù của lesson.html, không
phải dạng "câu hỏi + đáp án" — lesson.html vẫn giữ 2 câu đó cố định và nối
thêm vào cuối bộ đề random lấy từ API này.
"""
import json
import time

import database as db

# ── 34 câu trắc nghiệm gốc (di trú từ quizData trong lesson.html) ─────────
_LEGACY_QUESTIONS = [
    {"q": "Phản ứng tỏa nhiệt là phản ứng có:", "opts": ["ΔrH < 0, làm môi trường nóng lên", "ΔrH > 0, làm môi trường nóng lên", "ΔrH < 0, làm môi trường lạnh đi", "ΔrH > 0, làm môi trường lạnh đi"], "ans": 0},
    {"q": "Nhiệt tạo thành chuẩn (ΔfHo) của một đơn chất bền vững (VD: O2, H2) bằng:", "opts": ["< 0", "1", "0", "Phụ thuộc nhiệt độ"], "ans": 2},
    {"q": "Khi hòa tan Vôi sống (CaO) vào nước, cốc thủy tinh nóng lên rất nhanh. Phương trình nhiệt hóa học có:", "opts": ["ΔH > 0", "ΔH = 0", "ΔH < 0", "Không xác định được"], "ans": 2},
    {"q": "Ứng dụng của phản ứng thu nhiệt trong đời sống là:", "opts": ["Đốt than cháy", "Túi chườm lạnh y tế", "Túi sưởi ấm tay", "Pin điện hóa"], "ans": 1},
    {"q": "Cho PT: C(s) + O2(g) -> CO2(g)  ΔrH = -393.5 kJ. Khi đốt cháy 1 mol Carbon, hệ sẽ:", "opts": ["Hấp thụ 393.5 kJ", "Tỏa ra 393.5 kJ", "Tỏa ra 787 kJ", "Không đổi nhiệt"], "ans": 1},
    {"q": "Năng lượng liên kết càng lớn thì liên kết đó càng:", "opts": ["Kém bền", "Dễ đứt gãy", "Bền vững", "Dễ phản ứng"], "ans": 2},
    {"q": "Biến thiên Enthalpy của phản ứng có thể tính bằng:", "opts": ["Tổng năng lượng liên kết chất tham gia - Tổng NL liên kết sản phẩm", "Tổng khối lượng tham gia - sản phẩm", "Nhiệt độ lúc sau trừ lúc đầu", "Thể tích bình chứa"], "ans": 0},
    {"q": "Quá trình nước đá tan thành nước lỏng là quá trình:", "opts": ["Tỏa nhiệt", "Thu nhiệt", "Không có sự trao đổi nhiệt", "Phản ứng hóa học"], "ans": 1},
    {"q": "Điều kiện chuẩn của Enthalpy là:", "opts": ["0 độ C, 1 atm", "25 độ C, 1 bar", "100 độ C, 1 bar", "25 độ K, 1 atm"], "ans": 1},
    {"q": "Khi tăng nồng độ chất phản ứng, tốc độ phản ứng tăng do:", "opts": ["Tăng nhiệt độ hệ", "Tăng mật độ hạt, dẫn đến tăng số va chạm hiệu quả", "Giảm năng lượng hoạt hóa", "Tăng thể tích khí"], "ans": 1},
    {"q": "Hệ số nhiệt độ Van't Hoff (γ) bằng 2. Khi tăng nhiệt độ thêm 30 độ C, tốc độ phản ứng sẽ tăng:", "opts": ["2 lần", "6 lần", "8 lần", "16 lần"], "ans": 2},
    {"q": "Chất xúc tác làm tăng tốc độ phản ứng bằng cách:", "opts": ["Làm tăng nhiệt độ phản ứng", "Làm giảm năng lượng hoạt hóa", "Tăng nồng độ chất tham gia", "Tăng diện tích tiếp xúc"], "ans": 1},
    {"q": "Hành động nào sau đây là ứng dụng của việc tăng diện tích tiếp xúc?", "opts": ["Nấu chín thức ăn bằng nồi áp suất", "Bảo quản thực phẩm trong tủ lạnh", "Chẻ nhỏ củi trước khi đốt", "Quạt gió vào bếp lò"], "ans": 2},
    {"q": "Cho Mg vào HCl. Tốc độ phản ứng sẽ NHANH NHẤT khi dùng:", "opts": ["Mg dạng khối, HCl 1M, 20°C", "Mg dạng bột, HCl 1M, 20°C", "Mg dạng bột, HCl 2M, 50°C", "Mg dạng khối, HCl 2M, 50°C"], "ans": 2},
    {"q": "Bơm oxy nguyên chất vào bệnh nhân khó thở là ứng dụng tăng tốc độ quá trình hô hấp dựa trên yếu tố:", "opts": ["Chất xúc tác", "Nhiệt độ", "Áp suất / Nồng độ", "Diện tích tiếp xúc"], "ans": 2},
    {"q": "Phương trình v = k[A]^a[B]^b. Nếu hằng số tốc độ (k) rất lớn, phản ứng đó:", "opts": ["Xảy ra rất chậm", "Xảy ra rất nhanh", "Là phản ứng thu nhiệt", "Không bao giờ xảy ra"], "ans": 1},
    {"q": "Phản ứng quang hợp của cây xanh xảy ra nhanh hơn khi trời nắng. Yếu tố ảnh hưởng là:", "opts": ["Nồng độ CO2", "Nhiệt độ & Ánh sáng", "Chất xúc tác", "Áp suất"], "ans": 1},
    {"q": "Để hãm tốc độ phản ứng ôi thiu của thịt cá, người ta dùng cách:", "opts": ["Tăng nhiệt độ", "Thêm chất xúc tác", "Giảm nhiệt độ (bỏ tủ lạnh)", "Nghiền nhỏ thịt"], "ans": 2},
    {"q": "Trong nhóm Halogen, nguyên tố nào ở trạng thái Lỏng ở điều kiện thường?", "opts": ["Fluorine (F2)", "Chlorine (Cl2)", "Bromine (Br2)", "Iodine (I2)"], "ans": 2},
    {"q": "Nhiệt độ sôi của nhóm Halogen tăng dần từ F2 đến I2 do:", "opts": ["Lực Van der Waals tăng", "Khối lượng giảm", "Bán kính nguyên tử giảm", "Tính phi kim tăng"], "ans": 0},
    {"q": "Khí Halogen nào có màu vàng lục, rất độc và thường dùng khử trùng nước tiểu?", "opts": ["F2", "Cl2", "Br2", "I2"], "ans": 1},
    {"q": "Lực Van der Waals hình thành do:", "opts": ["Sự cho nhận electron hoàn toàn", "Sự dùng chung electron", "Sự xuất hiện lưỡng cực tạm thời và cảm ứng", "Lực hút lực đẩy hạt nhân"], "ans": 2},
    {"q": "Iodine (I2) có đặc tính vật lý nổi bật nào?", "opts": ["Chất lỏng màu nâu đỏ", "Chất rắn màu đen tím, dễ thăng hoa", "Khí không màu", "Khí màu vàng lục"], "ans": 1},
    {"q": "Độ âm điện của nhóm Halogen biến đổi thế nào từ F đến I?", "opts": ["Tăng dần", "Giảm dần", "Không đổi", "Tăng rồi giảm"], "ans": 1},
    {"q": "Lý do F2 không có dạng lỏng ở nhiệt độ thường là vì:", "opts": ["Khối lượng phân tử quá lớn", "Lực tương tác Van der Waals cực kỳ yếu", "Nó có liên kết ion", "Nó phản ứng với không khí"], "ans": 1},
    {"q": "Halogen có tính oxy hóa mạnh nhất là:", "opts": ["I2", "Br2", "Cl2", "F2"], "ans": 3},
    {"q": "Khi nhỏ dung dịch hồ tinh bột vào I2 sẽ xuất hiện màu:", "opts": ["Đỏ", "Xanh đen (tím than)", "Vàng", "Mất màu"], "ans": 1},
    {"q": "Theo định luật Avogadro, ở cùng T và P, hai thể tích khí bằng nhau sẽ có:", "opts": ["Cùng khối lượng", "Cùng số phân tử (số mol)", "Cùng khối lượng riêng", "Cùng màu sắc"], "ans": 1},
    {"q": "Thể tích mol của chất khí ở điều kiện chuẩn (25°C, 1 bar) là bao nhiêu Lít?", "opts": ["22.4 L", "24.79 L", "24.0 L", "22.7 L"], "ans": 1},
    {"q": "Bơm 10 Lít khí O2 và 10 Lít khí H2 vào 2 bình ở cùng đkc. Khẳng định nào đúng?", "opts": ["Bình O2 nặng hơn bình H2", "Bình H2 có nhiều phân tử hơn", "Cả 2 bình nặng bằng nhau", "Bình O2 có áp suất lớn hơn"], "ans": 0},
    {"q": "Nếu có 0.5 mol khí CO2 ở đkc, thể tích chiếm chỗ là:", "opts": ["11.2 L", "12.395 L", "24.79 L", "5.6 L"], "ans": 1},
    {"q": "Để 2 xilanh chứa khí N2 và O2 cân bằng số mol, ta cần:", "opts": ["Bơm thể tích N2 bằng với thể tích O2", "Bơm N2 nhiều hơn", "Bơm O2 nhiều hơn", "Không thể cân bằng"], "ans": 0},
    {"q": "Phản ứng nào sau đây thuộc loại phản ứng thu nhiệt?", "opts": ["Đốt cháy cồn", "Phân hủy đá vôi CaCO3", "Hòa tan acid sulfuric vào nước", "Oxy hóa quặng sắt"], "ans": 1},
    {"q": "Trộn 2 thể tích khí bằng nhau, khí A có M=2, khí B có M=32. Số hạt vi mô trong khí B bằng bao nhiêu lần khí A?", "opts": ["16 lần", "8 lần", "1 lần (bằng nhau)", "1/16 lần"], "ans": 2},
    {"q": "Nhận xét đúng về Lực Van der Waals:", "opts": ["Là lực liên kết hóa học rất mạnh", "Chỉ có ở chất khí", "Có bản chất tĩnh điện, rất yếu", "Cản trở sự bay hơi của nước"], "ans": 2},
    {"q": "Khi tăng nhiệt độ 40 độ C, v tăng 16 lần. Hỏi hệ số nhiệt độ γ là bao nhiêu?", "opts": ["1", "2", "3", "4"], "ans": 1},
]


def _row_to_dict(row) -> dict:
    return {
        'id': row['id'],
        'question': row['question'],
        'options': json.loads(row['options'] or '[]'),
        'answerIndex': row['answer_index'],
        'difficulty': row['difficulty'],
        'active': bool(row['active']),
        'createdBy': row['created_by'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


def seed_default_questions():
    """Chạy 1 lần lúc khởi động: nếu bảng quiz_questions đang trống, nạp 34
    câu gốc vào để admin thấy ngay và học sinh không bị mất đề đang có."""
    conn = db.get_conn()
    try:
        count = conn.execute('SELECT COUNT(*) c FROM quiz_questions').fetchone()['c']
    finally:
        conn.close()
    if count > 0:
        return 0
    ts = time.time()
    with db.tx() as conn:
        for item in _LEGACY_QUESTIONS:
            conn.execute(
                'INSERT INTO quiz_questions (question, options, answer_index, difficulty, active, '
                'created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)',
                (item['q'], json.dumps(item['opts'], ensure_ascii=False), item['ans'],
                 'TB', 'system_seed', ts, ts)
            )
    return len(_LEGACY_QUESTIONS)


def list_questions(include_inactive: bool = True) -> list[dict]:
    conn = db.get_conn()
    try:
        if include_inactive:
            rows = conn.execute('SELECT * FROM quiz_questions ORDER BY id DESC').fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM quiz_questions WHERE active = 1 ORDER BY id DESC'
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_question(qid: int) -> dict | None:
    conn = db.get_conn()
    try:
        row = conn.execute('SELECT * FROM quiz_questions WHERE id = ?', (qid,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def create_question(question: str, options: list[str], answer_index: int,
                     difficulty: str = 'TB', created_by: str = '') -> int:
    ts = time.time()
    with db.tx() as conn:
        cur = conn.execute(
            'INSERT INTO quiz_questions (question, options, answer_index, difficulty, active, '
            'created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)',
            (question, json.dumps(options, ensure_ascii=False), answer_index, difficulty, created_by, ts, ts)
        )
        return cur.lastrowid


def update_question(qid: int, question: str = None, options: list[str] = None,
                     answer_index: int = None, difficulty: str = None,
                     active: bool = None) -> bool:
    existing = get_question(qid)
    if not existing:
        return False
    new_question = existing['question'] if question is None else question
    new_options = existing['options'] if options is None else options
    new_answer = existing['answerIndex'] if answer_index is None else answer_index
    new_difficulty = existing['difficulty'] if difficulty is None else difficulty
    new_active = existing['active'] if active is None else active
    with db.tx() as conn:
        conn.execute(
            'UPDATE quiz_questions SET question = ?, options = ?, answer_index = ?, '
            'difficulty = ?, active = ?, updated_at = ? WHERE id = ?',
            (new_question, json.dumps(new_options, ensure_ascii=False), new_answer,
             new_difficulty, 1 if new_active else 0, time.time(), qid)
        )
    return True


def delete_question(qid: int) -> bool:
    with db.tx() as conn:
        cur = conn.execute('DELETE FROM quiz_questions WHERE id = ?', (qid,))
        return cur.rowcount > 0


def random_questions(count: int = 34) -> list[dict]:
    """Rút ngẫu nhiên `count` câu đang active — dùng ORDER BY RANDOM() của
    SQLite. Nếu ngân hàng có ít câu hơn `count`, trả về tất cả (đã xáo trộn)."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            'SELECT * FROM quiz_questions WHERE active = 1 ORDER BY RANDOM() LIMIT ?', (count,)
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]
