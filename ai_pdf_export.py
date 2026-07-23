"""
ai_pdf_export.py — Xuất 1 hội thoại AI (ai_conversations + ai_messages) ra
file PDF để học sinh tải về lưu lời giải (#5 "xuất PDF lời giải").

Dùng reportlab (thêm vào requirements.txt: `reportlab`) vì đây là thư viện
PDF thuần Python phổ biến nhất, không cần cài thêm hệ thống (khác wkhtmltopdf/
weasyprint vốn cần thư viện hệ thống, khó cài trên Render free tier).

FONT TIẾNG VIỆT — QUAN TRỌNG
=============================
Font PDF mặc định (Helvetica) của reportlab KHÔNG có dấu tiếng Việt. Cần 1
font Unicode (VD DejaVu Sans, Noto Sans) đặt tại đường dẫn dưới đây:

    fonts/DejaVuSans.ttf        (thường)
    fonts/DejaVuSans-Bold.ttf   (đậm)

Tải miễn phí tại https://dejavu-fonts.github.io/ rồi copy 2 file .ttf vào
thư mục `fonts/` cùng cấp với server.py, commit lên Git (Render không có
sẵn font hệ thống nào hỗ trợ tiếng Việt).

Nếu CHƯA có font (mới deploy lần đầu, chưa kịp thêm), module này tự động
fallback sang bỏ dấu tiếng Việt (không lỗi 500, nhưng lời giải xuất ra sẽ
không dấu) — xem `_ascii_fallback()`. Khi nào thêm font vào đúng đường dẫn,
PDF sẽ tự động có dấu mà không cần sửa code.
"""
import os
import re
import time
import unicodedata

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
_REGULAR_PATH = os.path.join(_FONT_DIR, 'DejaVuSans.ttf')
_BOLD_PATH = os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf')

_UNICODE_FONT_READY = False
_FONT_NAME = 'Helvetica'
_FONT_NAME_BOLD = 'Helvetica-Bold'


def _ensure_font_registered():
    """Đăng ký font Unicode 1 lần (idempotent). Trả về True nếu có font
    tiếng Việt thật, False nếu phải fallback về Helvetica (bỏ dấu)."""
    global _UNICODE_FONT_READY, _FONT_NAME, _FONT_NAME_BOLD
    if _UNICODE_FONT_READY:
        return True
    if os.path.exists(_REGULAR_PATH) and os.path.exists(_BOLD_PATH):
        try:
            pdfmetrics.registerFont(TTFont('CCVN', _REGULAR_PATH))
            pdfmetrics.registerFont(TTFont('CCVN-Bold', _BOLD_PATH))
            _FONT_NAME, _FONT_NAME_BOLD = 'CCVN', 'CCVN-Bold'
            _UNICODE_FONT_READY = True
            return True
        except Exception:
            pass
    return False


def _ascii_fallback(text: str) -> str:
    """Bỏ dấu tiếng Việt khi chưa có font Unicode — CHỈ dùng khi
    _ensure_font_registered() trả về False, để tránh PDF lỗi ký tự."""
    norm = unicodedata.normalize('NFD', text)
    stripped = ''.join(c for c in norm if unicodedata.category(c) != 'Mn')
    return stripped.replace('đ', 'd').replace('Đ', 'D')


def _esc(text: str) -> str:
    """Escape ký tự đặc biệt của reportlab Paragraph (dùng mini-HTML)."""
    text = (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return text.replace('\n', '<br/>')


def build_conversation_pdf(conversation: dict, messages: list[dict]) -> bytes:
    """Trả về nội dung PDF (bytes) của 1 hội thoại AI, gồm câu hỏi/trả lời
    theo đúng thứ tự thời gian. `conversation`/`messages` lấy từ
    firestore_db.get_conversation()/list_messages()."""
    has_unicode = _ensure_font_registered()
    font, font_bold = _FONT_NAME, _FONT_NAME_BOLD
    xform = (lambda s: s) if has_unicode else _ascii_fallback

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CCTitle', parent=styles['Title'], fontName=font_bold,
                                  fontSize=16, spaceAfter=4)
    meta_style = ParagraphStyle('CCMeta', parent=styles['Normal'], fontName=font,
                                 fontSize=9, textColor=colors.HexColor('#5B6477'), spaceAfter=14)
    role_style_user = ParagraphStyle('CCUser', parent=styles['Normal'], fontName=font_bold,
                                      fontSize=10.5, textColor=colors.HexColor('#4F6BFF'), spaceBefore=10, spaceAfter=3)
    role_style_model = ParagraphStyle('CCModel', parent=styles['Normal'], fontName=font_bold,
                                       fontSize=10.5, textColor=colors.HexColor('#06B6D4'), spaceBefore=10, spaceAfter=3)
    body_style = ParagraphStyle('CCBody', parent=styles['Normal'], fontName=font,
                                 fontSize=10.5, leading=15)

    story = [
        Paragraph(xform('ChemCraft — Lich su hoi thoai AI' if not has_unicode else 'ChemCraft — Lịch sử hội thoại AI'), title_style),
        Paragraph(xform(f"Chủ đề: {conversation.get('topic') or '(không có)'} · "
                         f"Bắt đầu lúc: {time.strftime('%H:%M %d/%m/%Y', time.localtime(conversation.get('started_at') or time.time()))}"),
                   meta_style),
        HRFlowable(width='100%', color=colors.HexColor('#E4E9F1'), thickness=1),
    ]

    for m in messages:
        role = m.get('role')
        content = xform(m.get('content') or '')
        if not content.strip():
            continue
        if role == 'user':
            story.append(Paragraph('Học sinh hỏi:' if has_unicode else xform('Hoc sinh hoi:'), role_style_user))
        else:
            story.append(Paragraph('ChemCraft AI trả lời:' if has_unicode else xform('ChemCraft AI tra loi:'), role_style_model))
        # Giữ nguyên xuống dòng của câu trả lời AI (thường có nhiều bước giải).
        safe = _esc(content)
        story.append(Paragraph(safe, body_style))

    if not messages:
        story.append(Spacer(1, 20))
        story.append(Paragraph(xform('(Hội thoại này chưa có nội dung.)'), body_style))

    doc.build(story)
    return buf.getvalue()
