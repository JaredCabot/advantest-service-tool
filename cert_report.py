"""ISO/IEC 17025-style calibration certificate generator (equipment-agnostic).

Renders a calibration certificate PDF from a plain results dictionary. The
layout, typography and colours are fixed by this module, so every certificate it
produces looks identical regardless of the instrument, the quantities measured or
the language.

Nothing here knows about any particular instrument: the caller supplies the
identification blocks, the reference standards, the result rows and the section
labels. See references/data_contract.md for the full input contract.

Entry point
-----------
``generate(data, lang, out_path)``
    Render one certificate to ``out_path``. ``lang`` selects the built-in string
    catalog ("en" or "ja"); ``data["strings"]`` overrides individual strings, and
    ``data["catalog"]`` supplies a complete catalog for any other language.
"""

from __future__ import annotations

import os
import sys
import math
import datetime
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                HRFlowable, PageBreak)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

# Candidate Japanese fonts to EMBED (glyph outlines), so the PDF renders Japanese
# in any viewer. (regular_path, subfont_index, bold_path, bold_subfont_index)
_JP_TTF_CANDIDATES = [
    (r"C:\Windows\Fonts\YuGothR.ttc", 0, r"C:\Windows\Fonts\YuGothB.ttc", 0),
    (r"C:\Windows\Fonts\YuGothM.ttc", 0, r"C:\Windows\Fonts\YuGothB.ttc", 0),
    (r"C:\Windows\Fonts\meiryo.ttc", 0, r"C:\Windows\Fonts\meiryob.ttc", 0),
    (r"C:\Windows\Fonts\msgothic.ttc", 0, r"C:\Windows\Fonts\msgothic.ttc", 0),
    (r"C:\Windows\Fonts\BIZ-UDGothicR.ttc", 0, r"C:\Windows\Fonts\BIZ-UDGothicB.ttc", 0),
]

_JP_FONTS: Tuple[str, str] = None  # (regular_font_name, bold_font_name), lazily set

def _ensure_jp_font() -> Tuple[str, str]:
    """Register a Japanese font and return ``(regular_name, bold_name)``.

    Prefers an embedded system TrueType face (glyph outlines travel inside the
    PDF, so it renders in any viewer); falls back to the non-embedded HeiseiMin
    CID font (renders only in viewers with Asian font support) if no TTF is
    found. The result is cached in :data:`_JP_FONTS` after the first call.
    """
    global _JP_FONTS
    if _JP_FONTS is not None:
        return _JP_FONTS
    for reg_path, reg_idx, bold_path, bold_idx in _JP_TTF_CANDIDATES:
        if not os.path.exists(reg_path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("JP", reg_path, subfontIndex=reg_idx))
            bname = "JP"
            if os.path.exists(bold_path):
                try:
                    pdfmetrics.registerFont(TTFont("JP-Bold", bold_path, subfontIndex=bold_idx))
                    bname = "JP-Bold"
                except Exception:
                    bname = "JP"
            _JP_FONTS = ("JP", bname)
            return _JP_FONTS
        except Exception:
            continue
    # fallback: non-embedded CID font
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    _JP_FONTS = ("HeiseiMin-W3", "HeiseiKakuGo-W5")
    return _JP_FONTS

# Built-in fallback report catalogs (English and Japanese). The lang/*.json
# files take precedence (see _catalog); these are used only if a language file
# is missing, so certificates still generate if the lang/ folder is deleted.
L: Dict[str, Dict[str, str]] = {
    "en": {
        "title": "CALIBRATION CERTIFICATE",
        "cert_no": "Certificate Number", "cal_date": "Date of Calibration",
        "issue_date": "Date of Issue", "page": "Page", "of": "of",
        "lab": "Calibration Laboratory", "customer": "Customer",
        "name": "Name", "address": "Address", "accreditation": "Accreditation",
        "uut": "Instrument Under Test", "description": "Description", "model": "Model",
        "serial": "Serial Number", "firmware": "Firmware", "options": "Options",
        "standards": "Reference Standards Used", "std_desc": "Description",
        "std_model": "Model", "std_serial": "Serial No.", "std_due": "Cal. Due",
        "std_trace": "Traceability / Cert. No.",
        "conditions": "Environmental Conditions", "temperature": "Temperature",
        "humidity": "Relative Humidity", "procedure": "Procedure",
        "test_type": "Type of Calibration",
        "results": "Measurement Results", "parameter": "Parameter",
        "frequency": "Frequency", "level": "Level", "nominal": "Nominal Value",
        "refval": "As-Used Value",
        "measured": "Measured Value", "deviation": "Deviation", "tolerance": "Tolerance (±)",
        "uncertainty": "Uncertainty (±)", "result": "Result",
        "pass": "PASS", "fail": "FAIL", "notspec": "Not specified",
        "na_opt002": "N/A", "nottested": "Not tested",
        "sec_freq": "Test Signal Frequency Accuracy",
        "sec_level": "Test Signal Level Accuracy",
        "sec_dcbias": "DC Bias Level Accuracy",
        "sec_cap": "Capacitance", "sec_res": "Resistance", "sec_dcr": "DC Resistance",
        "asfound": "As-Found Results (before adjustment)",
        "asleft": "As-Left Results (after adjustment)",
        "overall": "Overall Result",
        "unc_title": "Statement of Measurement Uncertainty",
        "unc_body": ("The reported expanded uncertainty of measurement is stated as the "
                     "standard uncertainty multiplied by a coverage factor k = 2, providing "
                     "a level of confidence of approximately 95%. It is derived from the "
                     "calibration uncertainties of the reference standards used and the "
                     "resolution and specified accuracy of the instrument under test."),
        "trace_title": "Statement of Traceability",
        "trace_body": ("The measurements reported in this certificate are traceable to national "
                       "measurement standards through the reference standards listed above."),
        "spec_note": ("Pass/Fail is assessed against the tolerance stated for each "
                      "measurement point."),
        "calby": "Calibrated by", "appby": "Approved by", "signature": "Signature",
        "date": "Date",
        "disclaimer": ("This certificate relates only to the item(s) calibrated and shall not be "
                       "reproduced other than in full without the written approval of the issuing "
                       "laboratory."),
        "type_asfound": "Performance Verification (As-Found)",
        "type_asleft": "Adjustment (As-Left)",
        "type_combined": "Calibration (As-Found & As-Left)",
    },
    "ja": {
        "title": "校正証明書",
        "cert_no": "証明書番号", "cal_date": "校正日",
        "issue_date": "発行日", "page": "ページ", "of": "/",
        "lab": "校正試験所", "customer": "お客様",
        "name": "名称", "address": "住所", "accreditation": "認定",
        "uut": "被校正器", "description": "品名", "model": "形名",
        "serial": "製造番号", "firmware": "ファームウェア",
        "options": "オプション",
        "standards": "使用した参照標準器", "std_desc": "品名",
        "std_model": "形名", "std_serial": "製造番号", "std_due": "校正期限",
        "std_trace": "トレーサビリティ / 証明書番号",
        "conditions": "環境条件", "temperature": "温度",
        "humidity": "相対湿度", "procedure": "手順",
        "test_type": "校正の種類",
        "results": "測定結果", "parameter": "パラメータ",
        "frequency": "周波数", "level": "レベル", "nominal": "公称値",
        "refval": "使用値",
        "measured": "測定値", "deviation": "偏差", "tolerance": "許容差 (±)",
        "uncertainty": "不確かさ (±)", "result": "判定",
        "pass": "合格", "fail": "不合格", "notspec": "規定なし",
        "na_opt002": "未試験", "nottested": "未試験",
        "sec_freq": "試験信号周波数確度",
        "sec_level": "試験信号レベル確度",
        "sec_dcbias": "DCバイアスレベル確度",
        "sec_cap": "静電容量", "sec_res": "抵抗", "sec_dcr": "直流抵抗",
        "asfound": "調整前の結果",
        "asleft": "調整後の結果",
        "overall": "総合判定",
        "unc_title": "測定不確かさに関する記述",
        "unc_body": ("報告した拡張不確かさは、標準不確かさに包含係数 k = 2 を乗じたもので、約 95% の信頼の水準に相当します。"
                     "これは、使用した参照標準器の校正不確かさ、および被校正器の分解能と規定精度に基づいています。"),
        "trace_title": "トレーサビリティに関する記述",
        "trace_body": ("本証明書に報告された測定は、上記の参照標準器を通じて国家測定標準にトレーサブルです。"),
        "spec_note": ("合格/不合格は、各測定点に規定された許容差に対して判定します。"),
        "calby": "校正実施者", "appby": "承認者", "signature": "署名",
        "date": "日付",
        "disclaimer": ("本証明書は校正した機器のみに関するもので、発行試験所の書面による承認なしに全体を除いて複製してはなりません。"
                       ),
        "type_asfound": "性能検証（調整前 / As-Found）",
        "type_asleft": "調整（調整後 / As-Left）",
        "type_combined": "校正（調整前・調整後 / As-Found・As-Left）",
    },
}


OHM = "Ω"  # Greek capital omega, the ohm symbol


def _num(x) -> str:
    """Format a number to 6 significant figures with a decimal point and comma
    thousands separators (e.g. 1,234.5).

    Scientific notation is never used: very small or very large magnitudes are
    expanded to plain fixed-point form (e.g. 0.0000029 rather than 2.9e-06),
    which reads more clearly on a certificate. The 6-significant-figure rounding
    is preserved via Decimal expansion of the %g result."""
    s = "%.6g" % x
    if "e" in s or "E" in s:
        s = format(Decimal(s), "f")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    intp, _, frac = s.partition(".")
    intp = format(int(intp or "0"), ",")
    return ("-" if neg else "") + intp + (("." + frac) if frac else "")


def _sigfig_decimals(u, sig: int = 2):
    """Number of decimal places to which a value should be rounded so its last
    digit sits in the same position as the ``sig``-significant-figure rounding of
    the uncertainty ``u`` (GUM JCGM 100:2008 sec. 7.2.6 / ILAC P14 sec. 6.3).

    May be negative (round to tens/hundreds). Returns ``None`` when ``u`` is not a
    usable positive magnitude, so the caller falls back to natural formatting."""
    if u is None:
        return None
    try:
        au = abs(float(u))
    except (TypeError, ValueError):
        return None
    if au == 0 or not math.isfinite(au):
        return None
    exp = math.floor(math.log10(au))          # position of the leading digit
    return (sig - 1) - exp                     # decimals to reach the last sig fig


def _fmt_fixed(v, d, unit: str = "") -> str:
    """Format ``v`` rounded to ``d`` decimal places (``d`` may be <= 0), in plain
    fixed-point with comma thousands separators and the unit appended. Used for
    the measured value, deviation and uncertainty so they read at the resolution
    justified by the uncertainty. ``None`` renders as ``"-"``."""
    if v is None:
        return "-"
    r = round(float(v), d) or 0.0             # 'or 0.0' collapses -0.0 to 0.0
    dd = d if d > 0 else 0
    s = "%.*f" % (dd, r)
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    intp, _, frac = s.partition(".")
    intp = format(int(intp or "0"), ",")
    s = ("-" if neg else "") + intp + (("." + frac) if frac else "")
    if unit in ("ohm", OHM, "Ω"):
        return s + " " + OHM
    return s + ((" " + unit) if unit else "")


def _fmt_val(v, unit: str = "") -> str:
    """Format a value using industry-standard engineering notation.

    Ohm values use the Omega symbol with k / M prefixes (for example 1.5 kΩ,
    100 kΩ, 1 MΩ); sub-ohm values stay in ohms (0.1 Ω). Other units keep their
    unit string. Numbers carry a decimal point and comma thousands separators.
    ``None`` renders as ``"-"``.
    """
    if v is None:
        return "-"
    if unit in ("ohm", OHM, "Ω"):
        a = abs(v)
        if a >= 1e6:
            return _num(v / 1e6) + " M" + OHM
        if a >= 1e3:
            return _num(v / 1e3) + " k" + OHM
        return _num(v) + " " + OHM
    if unit:
        return _num(v) + " " + unit
    return _num(v)


def _catalog(lang: str, overrides=None, catalog=None) -> Tuple[Dict[str, str], bool]:
    """Return ``(strings, cjk)`` for one language.

    Starts from the built-in catalog for ``lang`` (English if unknown), applies a
    caller-supplied complete ``catalog``, then applies individual ``overrides``.
    ``cjk`` selects the embedded CJK font pair; set ``data["cjk"] = True`` for a
    language that needs it.
    """
    tr = dict(L.get(lang) or L["en"])
    if catalog:
        tr.update(catalog)
    if overrides:
        tr.update(overrides)
    return tr, bool(tr.get("_cjk", lang == "ja"))


def _styles(cjk: bool):
    """Build the paragraph styles used across the certificate.

    Selects the CJK font pair when ``cjk`` is true, otherwise the Helvetica
    faces. Returns ``(styles_dict, base_font_name, bold_font_name)``.
    """
    if cjk:
        base, bold = _ensure_jp_font()
    else:
        base, bold = "Helvetica", "Helvetica-Bold"
    ss = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=17, leading=21, alignment=1),
        "h": ParagraphStyle("h", fontName=bold, fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=3),
        "n": ParagraphStyle("n", fontName=base, fontSize=8.5, leading=11),
        "small": ParagraphStyle("s", fontName=base, fontSize=7.5, leading=10),
        "cell": ParagraphStyle("c", fontName=base, fontSize=8, leading=10),
        "cellb": ParagraphStyle("cb", fontName=bold, fontSize=8, leading=10),
        # White bold header text: a Table's TEXTCOLOR style does NOT apply to
        # Paragraph flowables, so header cells must carry their own colour.
        "cellw": ParagraphStyle("cw", fontName=bold, fontSize=8, leading=10,
                                textColor=colors.white),
    }
    return ss, base, bold


def _kv_table(rows, ss, col1=42*mm):
    """Return a two-column key/value ``Table`` (bold key, plain value) with light
    rule lines, used for the identification and conditions blocks."""
    data = [[Paragraph(k, ss["cellb"]), Paragraph(v, ss["cell"])] for k, v in rows]
    t = Table(data, colWidths=[col1, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
    ]))
    return t


def _section_label(key, tr, labels=None):
    """Return the heading text for a row's ``section`` key.

    Resolution order: the caller's ``labels`` map, then a ``sec_<key>`` entry in
    the string catalog (so a translated catalog can name the sections), then the
    key itself. Returning None suppresses the heading row.
    """
    if labels and key in labels:
        return labels[key]
    catalog_key = "sec_%s" % key
    if catalog_key in tr:
        return tr[catalog_key]
    return str(key) if key is not None else None


#: Longest section, in data rows, that is held together on one page.
#:
#: A section that will not fit on a page cannot be kept together, so this is the
#: cut-off past which only the heading is tied to its first row. Every range on
#: an R6161 is five rows; the impedance sections of other equipment can be
#: longer, which is why there is a limit rather than an assumption.
SECTION_KEEP_MAX_ROWS = 10


def _results_table(rows, tr, ss, labels=None):
    """Return the measurement-results ``Table``.

    Each row dict may carry ``parameter``, ``freq_hz``/``is_dc``, ``nominal``,
    ``measured``, ``deviation``, ``tolerance``, ``uncertainty``, ``unit`` and
    ``passed`` (True/False/None). Pass rows are coloured green, fails red with a
    tinted background, and ``None`` renders as "not specified".
    """
    header = [tr["parameter"], tr["frequency"], tr["nominal"],
              tr.get("refval", "As-Used Value"), tr["measured"],
              tr["deviation"], tr["tolerance"], tr["uncertainty"], tr["result"]]
    ncols = len(header)
    base_font = ss["cell"].fontName
    bold_font = ss["cellb"].fontName

    # Build the plain-text grid first (header + one text list per row) so the
    # column widths can be sized to the actual content and NOTHING wraps -- in any
    # language. The result column (last) is bold, so it is measured with the bold
    # face; every other cell with the base face.
    def _cells(r):
        passed = r.get("passed")
        status = r.get("status")
        if status:
            res_txt = tr.get(status, tr["notspec"])
        else:
            res_txt = tr["notspec"] if passed is None else (tr["pass"] if passed else tr["fail"])
        freq = r.get("freq_hz")
        fstr = "DC" if (freq in (0, None) and r.get("is_dc")) else (
            "%g kHz" % (freq/1000.0) if freq and freq >= 1000 else ("%g Hz" % freq if freq else "-"))
        unit = r.get("unit", "")
        # Group key marks which visual sub-section this row belongs to. Rows sharing
        # a key are drawn as one block; a thicker rule is placed where the key
        # changes. Fall back to the parameter name when no explicit group is given
        # (with the dissipation factor "D" merged into the preceding "Cp" block so
        # a capacitance measurement and its D stay together).
        grp = r.get("group")
        if grp is None:
            p = r.get("parameter", "")
            grp = "Cp" if p == "D" else p
        # GUM/ILAC presentation: quote the expanded uncertainty to 2 significant
        # figures and round the measured value and deviation to that same decimal
        # place. Nominal, as-used and tolerance keep their natural resolution
        # (they are assigned/spec values, not the measurement result). Grading is
        # unaffected -- it uses the full-precision values, not these strings.
        u = r.get("uncertainty")
        d = _sigfig_decimals(u)

        def _m(v):
            return _fmt_fixed(v, d, unit) if d is not None else _fmt_val(v, unit)
        return ([r.get("parameter", ""), fstr,
                 _fmt_val(r.get("nominal"), unit), _fmt_val(r.get("standard"), unit),
                 _m(r.get("measured")), _m(r.get("deviation")),
                 _fmt_val(r.get("tolerance"), unit), _m(u),
                 res_txt], passed, grp, r.get("section"))
    text_rows = [_cells(r) for r in rows]

    BASE_FS = 8.0
    def _w1(s, bold):
        try:
            return pdfmetrics.stringWidth(s or "", bold_font if bold else base_font, BASE_FS)
        except Exception:
            return len(s or "") * BASE_FS * 0.62
    def _w(s, bold):
        # A cell may carry a <br/> line break (e.g. the compact "Not<br/>tested"
        # result). Size the column to the widest single line, not the whole
        # literal, so the break actually keeps the column narrow.
        s = s or ""
        if "<br/>" in s:
            return max(_w1(part, bold) for part in s.split("<br/>"))
        return _w1(s, bold)
    textw = []
    for c in range(ncols):
        widest = _w(header[c], True)
        for texts, _, _, _ in text_rows:
            widest = max(widest, _w(texts[c], c == ncols - 1))
        textw.append(widest)

    # Column width = text width + horizontal padding (+ a small safety margin).
    # Grow the columns to fill the page when they fit; otherwise shrink the font
    # just enough that the widest content still fits on one line.
    PAD, EPS = 6.0, 1.5          # 3 pt left + 3 pt right padding, per column
    avail = A4[0] - 32*mm - 2    # usable width inside the 16 mm margins, less a hair
    text_total = sum(textw) or 1.0
    if text_total + ncols*(PAD+EPS) <= avail:
        fs = BASE_FS
        raw = [w + PAD + EPS for w in textw]
        scale = avail / sum(raw)
        colw = [r*scale for r in raw]
    else:
        scale_text = (avail - ncols*(PAD+EPS)) / text_total
        fs = BASE_FS * scale_text
        colw = [w*scale_text + PAD + EPS for w in textw]

    lead = fs * 1.25
    cst = ParagraphStyle("rc",  parent=ss["cell"],  fontSize=fs, leading=lead)
    cbst = ParagraphStyle("rcb", parent=ss["cellb"], fontSize=fs, leading=lead)
    cwst = ParagraphStyle("rcw", parent=ss["cellw"], fontSize=fs, leading=lead)
    csec = ParagraphStyle("rcs", parent=ss["cellb"], fontSize=fs, leading=lead,
                          textColor=colors.HexColor("#1F3864"))

    data = [[Paragraph(h, cwst) for h in header]]
    last = ncols - 1
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAAAAA")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    # Build the body row by row, tracking the actual table-row index (`ri`) because
    # section-heading rows are interleaved with data rows. A heading row spans the
    # full width, is shaded, and labels the section (test, or impedance cable
    # length + reading type). Within a section, a thicker rule still separates the
    # individual sub-groups (each standard/condition).
    ri = 0
    prev_grp = None
    prev_section = None
    # Where each section starts, so a whole section can be held together rather
    # than only its heading and first row; see the NOSPLIT pass below.
    section_spans = []
    for texts, passed, grp, section in text_rows:
        if section is not None and section != prev_section:
            ri += 1
            label = _section_label(section, tr, labels) or ""
            row = [Paragraph(label, csec)] + ["" for _ in range(ncols - 1)]
            data.append(row)
            style.append(("SPAN", (0, ri), (-1, ri)))
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#E8ECF4")))
            style.append(("LINEABOVE", (0, ri), (-1, ri), 1.1, colors.HexColor("#5A5A5A")))
            # Slightly shorter than a data row so headings stay compact.
            style.append(("TOPPADDING", (0, ri), (-1, ri), 1.8))
            style.append(("BOTTOMPADDING", (0, ri), (-1, ri), 1.8))
            # Never let a heading be the last row on a page: tie it to its first
            # data row so, if they do not both fit, they move to the next page
            # together (prevents an orphaned sub-heading).
            style.append(("NOSPLIT", (0, ri), (-1, ri + 1)))
            section_spans.append([ri, ri])
            prev_section = section
            prev_grp = None
        ri += 1
        if section_spans:
            section_spans[-1][1] = ri
        data.append([Paragraph(texts[j], cbst if j == last else cst) for j in range(ncols)])
        # Thicker rule above the first row of each new sub-group within a section.
        if prev_grp is not None and grp != prev_grp:
            style.append(("LINEABOVE", (0, ri), (-1, ri), 1.1, colors.HexColor("#5A5A5A")))
        prev_grp = grp
        if passed is True:
            style.append(("TEXTCOLOR", (last, ri), (last, ri), colors.HexColor("#1B7A2E")))
        elif passed is False:
            style.append(("TEXTCOLOR", (last, ri), (last, ri), colors.HexColor("#B00020")))
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#F2D2D5")))
    # Hold each section together on one page, not merely its heading and first
    # row. A range split across a page boundary reads as two different sets of
    # results, and a reader checking one range has to turn the page mid-block.
    #
    # Bounded deliberately. A section taller than a page cannot be held together
    # at all, and asking reportlab to do it would either overflow the frame or
    # fail the build, so anything longer than SECTION_KEEP_MAX_ROWS keeps only
    # the heading tied to its first row -- the original behaviour. Ten rows is
    # comfortably above the five a range needs and well below a page.
    for start, end in section_spans:
        if end > start and (end - start) <= SECTION_KEEP_MAX_ROWS:
            style.append(("NOSPLIT", (0, start), (-1, end)))

    t = Table(data, colWidths=colw, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def generate(data: Dict[str, Any], lang: str, out_path: str) -> str:
    """Render a single-language calibration certificate PDF to ``out_path``.

    ``data`` is the results dictionary assembled by the service tool (identity,
    lab/customer, reference standards, environmental conditions, as-found /
    results / as-left tables, signatures). ``lang`` selects the report catalog
    (see :func:`_catalog`). Returns ``out_path``.
    """
    tr, cjk = _catalog(lang, data.get("strings"), data.get("catalog"))
    labels = data.get("section_labels") or {}
    if cjk:
        _ensure_jp_font()
    ss, base, bold = _styles(cjk)
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=16*mm, rightMargin=16*mm,
                            topMargin=14*mm, bottomMargin=16*mm,
                            title="%s - %s" % (tr["title"], data.get("uut", {}).get("serial", "")))
    story = []
    story.append(Paragraph(tr["title"], ss["title"]))
    story.append(Spacer(1, 3))
    story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#1F3864")))
    story.append(Spacer(1, 4))

    # top identification block
    ttkey = data.get("test_type_key")
    ttlabel = tr.get(ttkey, data.get("test_type_label", "-")) if ttkey else data.get("test_type_label", "-")
    top = [(tr["cert_no"], data.get("cert_number", "-")),
           (tr["cal_date"], data.get("cal_date", "-")),
           (tr["issue_date"], data.get("issue_date", "-")),
           (tr["test_type"], ttlabel)]
    story.append(_kv_table(top, ss))
    story.append(Spacer(1, 4))

    # lab + customer side by side
    lab = data.get("lab", {}); cust = data.get("customer", {})
    lab_rows = [(tr["name"], lab.get("name", "-")), (tr["address"], lab.get("address", "-")),
                (tr["accreditation"], lab.get("accreditation", "-"))]
    cust_rows = [(tr["name"], cust.get("name", "-")), (tr["address"], cust.get("address", "-"))]
    lc = Table([[Paragraph(tr["lab"], ss["h"]), Paragraph(tr["customer"], ss["h"])],
                [_kv_table(lab_rows, ss, 26*mm), _kv_table(cust_rows, ss, 26*mm)]],
               colWidths=[None, None])
    lc.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(lc)
    story.append(Spacer(1, 2))

    # UUT
    story.append(Paragraph(tr["uut"], ss["h"]))
    u = data.get("uut", {})
    story.append(_kv_table([(tr["description"], u.get("description", "-")),
                            (tr["model"], u.get("model", "-")),
                            (tr["serial"], u.get("serial", "-")),
                            (tr["firmware"], u.get("firmware", "-")),
                            (tr["options"], u.get("options", "-"))], ss))

    # standards
    stds = data.get("standards", [])
    if stds:
        story.append(Paragraph(tr["standards"], ss["h"]))
        hdr = [tr["std_desc"], tr["std_model"], tr["std_serial"], tr["std_due"], tr["std_trace"]]
        sd = [[Paragraph(h, ss["cellw"]) for h in hdr]]
        for s in stds:
            sd.append([Paragraph(s.get("description", "-"), ss["cell"]),
                       Paragraph(s.get("model", "-"), ss["cell"]),
                       Paragraph(s.get("serial", "-"), ss["cell"]),
                       Paragraph(s.get("cal_due", "-"), ss["cell"]),
                       Paragraph(s.get("traceability", "-"), ss["cell"])])
        st = Table(sd, colWidths=[46*mm, 28*mm, 28*mm, 24*mm, None], repeatRows=1)
        st.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAAAAA")),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(st)

    # conditions
    cond = data.get("conditions", {})
    story.append(Paragraph(tr["conditions"], ss["h"]))
    story.append(_kv_table([(tr["temperature"], "%s C" % cond.get("temperature_c", "-")),
                            (tr["humidity"], "%s %%RH" % cond.get("humidity_pct", "-")),
                            (tr["procedure"], cond.get("procedure", "-"))], ss))

    # results -- the As-Found and As-Left sections each start on a new page and may
    # flow across several pages.
    for key, title in (("as_found_results", tr["asfound"]),
                       ("results", tr["results"]),
                       ("as_left_results", tr["asleft"])):
        rows = data.get(key)
        if rows:
            if key in ("as_found_results", "as_left_results"):
                story.append(PageBreak())
            story.append(Paragraph(title, ss["h"]))
            story.append(_results_table(rows, tr, ss, labels))

    # The overall result and the closing statements / signatures always start on
    # a fresh page, so the sign-off reads as a distinct summary page.
    story.append(PageBreak())

    # overall
    ov = data.get("overall_pass")
    if ov is not None:
        txt = tr["pass"] if ov else tr["fail"]
        col = "#1B7A2E" if ov else "#B00020"
        story.append(Paragraph('<b>%s: <font color="%s">%s</font></b>' % (tr["overall"], col, txt), ss["n"]))

    story.append(Spacer(1, 6))
    note = data.get("spec_note") or tr.get("spec_note")
    if note:
        story.append(Paragraph(note, ss["small"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(tr["unc_title"], ss["h"]))
    story.append(Paragraph(tr["unc_body"], ss["small"]))
    story.append(Paragraph(tr["trace_title"], ss["h"]))
    story.append(Paragraph(tr["trace_body"], ss["small"]))

    # signatures
    story.append(Spacer(1, 10))
    sig = [[Paragraph("%s: %s" % (tr["calby"], data.get("operator", "")), ss["n"]),
            Paragraph("%s: %s" % (tr["appby"], data.get("approver", "")), ss["n"])],
           [Paragraph(tr["signature"] + ": ____________________", ss["n"]),
            Paragraph(tr["signature"] + ": ____________________", ss["n"])],
           [Paragraph(tr["date"] + ": ____________________", ss["n"]),
            Paragraph(tr["date"] + ": ____________________", ss["n"])]]
    sigt = Table(sig, colWidths=[None, None])
    sigt.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 6)]))
    story.append(sigt)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#AAAAAA")))
    story.append(Paragraph(data.get("disclaimer") or tr["disclaimer"], ss["small"]))

    def _footer(canvas, docu):
        canvas.saveState()
        canvas.setFont(base, 7.5)
        cert = data.get("cert_number", "")
        canvas.drawString(16*mm, 8*mm, "%s %s" % (tr["cert_no"], cert))
        canvas.drawRightString(A4[0]-16*mm, 8*mm, "%s %d" % (tr["page"], docu.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path


def generate_both(data: Dict[str, Any], out_dir: str, basename: str) -> Tuple[str, str]:
    """Render the English and Japanese certificates for ``data`` into ``out_dir``.

    Returns ``(english_pdf_path, japanese_pdf_path)``. This is a convenience
    wrapper around :func:`generate`; the service tool itself iterates
    ``REPORT_LANGUAGES`` and calls :func:`generate` directly.
    """
    os.makedirs(out_dir, exist_ok=True)
    en = os.path.join(out_dir, basename + "_EN.pdf")
    ja = os.path.join(out_dir, basename + "_JA.pdf")
    generate(data, "en", en)
    generate(data, "ja", ja)
    return en, ja


# --------------------------------------------------------------------------- #
# Row helper
# --------------------------------------------------------------------------- #
def make_row(parameter, nominal, measured, tolerance, uncertainty, unit="",
             freq_hz=None, standard=None, group=None, section=None,
             is_dc=False, status=None, reference=None):
    """Build one graded results row.

    Every numeric argument must already be in the SAME display unit (``unit``),
    because the certificate prints them side by side: a 100 mV point is passed as
    nominal=100, unit="mV", not as 0.1 V. Deviation and the verdict are computed
    here, from ``reference`` when given (the calibrated value of the standard
    actually used) and from ``nominal`` otherwise.

    Pass ``status`` (a catalog key such as "nottested") for a point that was not
    measured: the Result column shows that text and the row is not graded, so it
    cannot affect the overall verdict.
    """
    target = reference if reference is not None else (
        standard if standard is not None else nominal)
    dev = None if (measured is None or target is None) else (measured - target)
    passed = None if (tolerance is None or dev is None) else (abs(dev) <= tolerance)
    if status:
        passed = None
    return dict(parameter=parameter, unit=unit,
                freq_hz=(int(freq_hz) if freq_hz else None), is_dc=is_dc,
                nominal=nominal, standard=standard, measured=measured,
                deviation=dev, tolerance=tolerance, uncertainty=uncertainty,
                passed=passed, group=group, section=section, status=status)


def overall_pass(rows):
    """AND of every graded row; None when nothing in ``rows`` is graded."""
    graded = [r for r in (rows or []) if r.get("passed") is not None]
    return all(r["passed"] for r in graded) if graded else None
