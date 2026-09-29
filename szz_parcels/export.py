"""Таблицы в Excel (xlsx) и Word (docx) без сторонних библиотек: файлы Office Open XML.

Таблица — словарь: title (заголовок), headers, widths (доли ширины), rows
(списки значений: строка, число или None), decimals (знаков у чисел по столбцам).
"""

import zipfile
from xml.sax.saxutils import escape


def _x(value):
    return escape(str(value), {'"': "&quot;"})


def _cell_text(value, decimals):
    if value is None:
        return ""
    if isinstance(value, float):
        return ("{:.%df}" % decimals).format(value).replace(".", ",")
    return str(value)


def _write(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)


# ---------------------------------------------------------------- Excel

def _col(index):
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _sheet_name(title, used):
    name = "".join(ch for ch in title if ch not in '[]:*?/\\')[:31] or "Лист"
    base, n = name, 2
    while name in used:
        suffix = " ({})".format(n)
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name)
    return name


def write_xlsx(path, tables):
    """Каждая таблица — отдельный лист: строка заголовков закреплена, у неё фильтр."""
    # стили: 0 — обычный, 1 — заголовок, 2 — текст в рамке, 3.. — числа в рамке по точности
    decimals_used = sorted({d for t in tables for d in t.get("decimals", {}).values()})
    num_formats = "".join(
        '<numFmt numFmtId="{}" formatCode="{}"/>'.format(
            164 + i, "0" if d == 0 else "0." + "0" * d)
        for i, d in enumerate(decimals_used))
    num_xfs = "".join(
        '<xf numFmtId="{}" fontId="0" fillId="0" borderId="1" applyNumberFormat="1" '
        'applyBorder="1" applyAlignment="1"><alignment vertical="top"/></xf>'.format(164 + i)
        for i in range(len(decimals_used)))
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="{nf}">{num_formats}</numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE7EEF7"/></patternFill></fill></fills>'
        '<borders count="2"><border/><border><left style="thin"/><right style="thin"/>'
        '<top style="thin"/><bottom style="thin"/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="{nx}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" '
        'applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" '
        'wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>{num_xfs}</cellXfs>'
        '</styleSheet>').format(nf=len(decimals_used), num_formats=num_formats,
                                nx=3 + len(decimals_used), num_xfs=num_xfs)

    files = []
    sheets = []
    used = set()
    for index, table in enumerate(tables, 1):
        name = _sheet_name(table.get("sheet") or table["title"], used)
        sheets.append(name)
        headers = table["headers"]
        widths = table.get("excel_widths") or [18] * len(headers)
        decimals = table.get("decimals", {})
        cols = "".join('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w)
                       for i, w in enumerate(widths))
        rows = ['<row r="1">' + "".join(
            '<c r="{}1" t="inlineStr" s="1"><is><t>{}</t></is></c>'.format(_col(i), _x(h))
            for i, h in enumerate(headers)) + "</row>"]
        for r, values in enumerate(table["rows"], 2):
            cells = []
            for i, value in enumerate(values):
                ref = "{}{}".format(_col(i), r)
                if value is None:
                    cells.append('<c r="{}" s="2"/>'.format(ref))
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    style = 3 + decimals_used.index(decimals[i]) if i in decimals else 2
                    number = round(value, decimals[i]) if i in decimals else value
                    cells.append('<c r="{}" s="{}"><v>{}</v></c>'.format(ref, style, repr(number)))
                else:
                    cells.append('<c r="{}" t="inlineStr" s="2"><is><t xml:space="preserve">{}'
                                 '</t></is></c>'.format(ref, _x(value)))
            rows.append('<row r="{}">{}</row>'.format(r, "".join(cells)))
        last = "{}{}".format(_col(len(headers) - 1), max(1, len(table["rows"]) + 1))
        sheet = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            '<cols>{}</cols><sheetData>{}</sheetData><autoFilter ref="A1:{}"/>'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
            '<pageSetup paperSize="9" orientation="portrait" fitToHeight="0"/>'
            '</worksheet>').format(cols, "".join(rows), last)
        files.append(("xl/worksheets/sheet{}.xml".format(index), sheet))

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(_x(n), i, i)
                  for i, n in enumerate(sheets, 1))
        + '</sheets><definedNames>'
        + "".join('<definedName name="_xlnm._FilterDatabase" localSheetId="{}" hidden="1">'
                  "'{}'!$A$1:${}${}</definedName>".format(
                      i, _x(n).replace("'", "''"), _col(len(t["headers"]) - 1),
                      max(1, len(t["rows"]) + 1))
                  for i, (n, t) in enumerate(zip(sheets, tables)))
        + '</definedNames></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join('<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/'
                      'officeDocument/2006/relationships/worksheet" '
                      'Target="worksheets/sheet{0}.xml"/>'.format(i)
                      for i in range(1, len(sheets) + 1))
            + '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/'
              '2006/relationships/styles" Target="styles.xml"/></Relationships>'
            .format(len(sheets) + 1))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        + "".join('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="application/'
                  'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i)
                  for i in range(1, len(sheets) + 1))
        + '</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    _write(path, [("[Content_Types].xml", content_types), ("_rels/.rels", root_rels),
                  ("xl/workbook.xml", workbook), ("xl/_rels/workbook.xml.rels", rels),
                  ("xl/styles.xml", styles)] + files)


# ---------------------------------------------------------------- Word

# A4 книжная; поля: левое 30 мм, правое 15 мм, верхнее и нижнее 20 мм (в twip, 1 мм = 56,7)
PAGE_W, PAGE_H = 11906, 16838
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 1701, 850, 1134, 1134
TEXT_W = PAGE_W - MARGIN_L - MARGIN_R


def _run(text, bold=False, size=None):
    props = ""
    if bold:
        props += "<w:b/>"
    if size:
        props += '<w:sz w:val="{0}"/><w:szCs w:val="{0}"/>'.format(size * 2)
    return '<w:r>{}<w:t xml:space="preserve">{}</w:t></w:r>'.format(
        "<w:rPr>{}</w:rPr>".format(props) if props else "", _x(text))


def _para(text="", bold=False, size=None, align=None, keep_next=False, after=120, style=None):
    props = ""
    if style:
        props += '<w:pStyle w:val="{}"/>'.format(style)
    if keep_next:
        props += "<w:keepNext/>"
    props += '<w:spacing w:before="0" w:after="{}"/>'.format(after)
    if align:
        props += '<w:jc w:val="{}"/>'.format(align)
    return "<w:p><w:pPr>{}</w:pPr>{}</w:p>".format(props, _run(text, bold, size) if text else "")


def _table(table):
    widths = table["widths"]
    total = float(sum(widths))
    twips = [int(TEXT_W * w / total) for w in widths]
    twips[-1] += TEXT_W - sum(twips)
    decimals = table.get("decimals", {})
    grid = "".join('<w:gridCol w:w="{}"/>'.format(w) for w in twips)
    border = '<w:{} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
    borders = "".join(border.format(side)
                      for side in ("top", "left", "bottom", "right", "insideH", "insideV"))

    def row(values, header=False):
        cells = []
        for i, value in enumerate(values):
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            align = "center" if header or i == 0 or numeric else "left"
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7EEF7"/>' if header else ""
            cells.append(
                '<w:tc><w:tcPr><w:tcW w:w="{}" w:type="dxa"/>{}</w:tcPr>'
                '<w:p><w:pPr><w:spacing w:before="0" w:after="0"/><w:jc w:val="{}"/></w:pPr>{}'
                '</w:p></w:tc>'.format(
                    twips[i], shade, align,
                    _run(_cell_text(value, decimals.get(i, 0)), bold=header, size=10)))
        props = "<w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>" if header else \
            "<w:trPr><w:cantSplit/></w:trPr>"
        return "<w:tr>{}{}</w:tr>".format(props, "".join(cells))

    body = [row(table["headers"], header=True)] + [row(values) for values in table["rows"]]
    return ('<w:tbl><w:tblPr><w:tblW w:w="{}" w:type="dxa"/><w:tblBorders>{}</w:tblBorders>'
            '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="57" w:type="dxa"/>'
            '<w:right w:w="57" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>{}</w:tblGrid>'
            '{}</w:tbl>').format(TEXT_W, borders, grid, "".join(body))


def write_docx(path, title, notes, tables):
    """Документ A4: заголовок, пояснения (список строк), таблицы с подписями."""
    body = [_para(title, bold=True, size=14, align="center", after=240)]
    for note in notes:
        body.append(_para(note, size=12, align="both"))
    for table in tables:
        body.append(_para("", after=0))
        body.append(_para(table["title"], size=12, keep_next=True, after=120))
        if table["rows"]:
            body.append(_table(table))
        else:
            body.append(_para(table.get("empty") or "Участков нет.", size=12))
    body.append(
        '<w:sectPr><w:pgSz w:w="{}" w:h="{}"/><w:pgMar w:top="{}" w:right="{}" w:bottom="{}" '
        'w:left="{}" w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'.format(
            PAGE_W, PAGE_H, MARGIN_T, MARGIN_R, MARGIN_B, MARGIN_L))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>{}</w:body></w:document>').format("".join(body))
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" '
        'w:hAnsi="Times New Roman" w:cs="Times New Roman" w:eastAsia="Times New Roman"/>'
        '<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        '</w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
        '</w:style></w:styles>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.styles+xml"/></Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/></Relationships>')
    _write(path, [("[Content_Types].xml", content_types), ("_rels/.rels", root_rels),
                  ("word/document.xml", document), ("word/_rels/document.xml.rels", doc_rels),
                  ("word/styles.xml", styles)])
