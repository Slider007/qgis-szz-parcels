"""Карта СЗЗ: макет A3 альбомный «Карта СЗЗ» в рамке ГОСТ (форма 3 со штампом) и PDF.

Карта показывает слои проекта как они включены (подложка «как в проекте»), охват — зона
поиска с полями. Легенда — только слои реестра: граница предприятия, СЗЗ, участки в СЗЗ и
рядом, зона поиска. Объекты QGIS держатся в переменных: цепочки вида
layer.labeling().settings().format() в 3.44 роняют QGIS (временный объект удаляется раньше).
"""

import datetime
import os

from qgis.core import (
    Qgis,
    QgsFillSymbol,
    QgsLayoutExporter,
    QgsMapLayerLegendUtils,
    QgsMapLayerType,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutItemShape,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPalLayerSettings,
    QgsPrintLayout,
    QgsReadWriteContext,
    QgsRectangle,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtXml import QDomDocument

LAYOUT_NAME = "Карта СЗЗ"
ZONING_LAYOUT = "Карта градостроительного зонирования"
BORDER_LAYOUT = "Схема границы СЗЗ"
TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "forma_3.qpt")
PAGE_W, PAGE_H = 420.0, 297.0  # A3 альбомный, мм
FRAME_LEFT, FRAME = 20.0, 5.0  # поля рамки ГОСТ 2.301
FIELD_W, FIELD_H = PAGE_W - FRAME_LEFT - FRAME, PAGE_H - 2 * FRAME
STAMP_LEFT, STAMP_TOP = 230.0, 237.0  # основная надпись 185×55 мм в правом нижнем углу
SIDE = 70.0  # колонка линейки и примечания у штампа
MM = Qgis.LayoutUnit.Millimeters
NORTH_ARROW = ":/images/north_arrows/layout_default_north_arrow.svg"


def _outline(layer, color, width, style="solid", fill="0,0,0,0"):
    symbol = QgsFillSymbol.createSimple({"color": fill, "outline_color": color,
                                         "outline_style": style, "outline_width": str(width)})
    renderer = layer.renderer()
    if renderer is not None:
        renderer.setSymbol(symbol)
    layer.triggerRepaint()


def style_search_zone(layer):
    """Зона поиска — серый пунктир без заливки: синий — у границы предприятия."""
    _outline(layer, "110,110,110", 0.5, "dash")


def style_enterprise(layer):
    """Граница предприятия, загруженная по номерам, — сплошная синяя линия по внешнему контуру:
    несколько участков рисуются одним объектом, стыки между ними не видны (пожелание 2026-10-27:
    красная сливалась с красными участками в СЗЗ)."""
    from qgis.core import QgsMergedFeatureRenderer, QgsSingleSymbolRenderer
    symbol = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "0,60,220",
                                         "outline_style": "solid", "outline_width": "1"})
    layer.setRenderer(QgsMergedFeatureRenderer(QgsSingleSymbolRenderer(symbol)))
    layer.triggerRepaint()


def style_szz(layer):
    """Построенная СЗЗ (сглаженная или размером) — пунктир с мелкими «СЗЗ» вдоль границы,
    как ЗОУИТ на планах (пожелание 2026-10-27). Надписи — подписями слоя, а не значками линии:
    подписи QGIS сам ставит читаемо (значки на нижней стороне шли вверх ногами) и не тащит в легенду."""
    from qgis.core import QgsSimpleLineSymbolLayer
    color = QColor(130, 0, 170)
    symbol = QgsFillSymbol()
    symbol.deleteSymbolLayer(0)
    line = QgsSimpleLineSymbolLayer(color, 0.5)
    line.setUseCustomDashPattern(True)
    line.setCustomDashVector([4.0, 2.0])
    symbol.appendSymbolLayer(line)
    renderer = layer.renderer()
    if renderer is not None:
        renderer.setSymbol(symbol)
    settings = QgsPalLayerSettings()
    settings.isExpression = True
    settings.fieldName = "'СЗЗ'"
    settings.placement = Qgis.LabelPlacement.Line  # у полигона — вдоль контура
    settings.repeatDistance = 30
    settings.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    settings.displayAll = True  # не прятать при наложении на подписи участков
    text = QgsTextFormat()
    font = QFont("Arial")
    font.setPointSizeF(6)
    text.setFont(font)
    text.setSize(6)
    text.setColor(color)
    settings.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def style_normed(layer):
    """Нормируемые территории в СЗЗ — красная штриховка поверх заливки участков в СЗЗ."""
    from qgis.core import QgsLinePatternFillSymbolLayer, QgsSimpleLineSymbolLayer
    symbol = QgsFillSymbol()
    symbol.deleteSymbolLayer(0)
    hatch = QgsLinePatternFillSymbolLayer()
    hatch.setLineAngle(45)
    hatch.setDistance(1.5)
    hatch.setColor(QColor(160, 0, 0))
    hatch.setLineWidth(0.3)
    symbol.appendSymbolLayer(hatch)
    border = QgsSimpleLineSymbolLayer(QColor(160, 0, 0), 0.6)
    symbol.appendSymbolLayer(border)
    renderer = layer.renderer()
    if renderer is not None:
        renderer.setSymbol(symbol)
    layer.triggerRepaint()


def style_points(layer):
    """Характерные точки границы СЗЗ: чёрные кружки с номером."""
    from qgis.core import QgsMarkerSymbol
    symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0", "size": "1.4",
                                           "outline_color": "255,255,255", "outline_width": "0.2"})
    renderer = layer.renderer()
    if renderer is not None:
        renderer.setSymbol(symbol)
    settings = QgsPalLayerSettings()
    settings.fieldName = "num"
    text = QgsTextFormat()
    font = QFont()
    font.setPointSizeF(7)
    font.setBold(True)
    text.setFont(font)
    text.setSize(7)
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.8)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    settings.setFormat(text)
    settings.placement = Qgis.LabelPlacement.AroundPoint
    settings.dist = 1.0
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def label_cadastral_end(layer, field="cad_num"):
    """Подпись — конец кадастрового номера («:3056»), как на карте НСПД."""
    settings = QgsPalLayerSettings()
    settings.isExpression = True
    settings.fieldName = "regexp_substr(\"{}\", ':[0-9]+$')".format(field)
    text = QgsTextFormat()
    font = QFont()
    font.setPointSizeF(7)
    text.setFont(font)
    text.setSize(7)
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.8)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    settings.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def _label(layout, text, x, y, w, h, size, bold=False):
    item = QgsLayoutItemLabel(layout)
    item.setText(text)
    font = QFont()
    font.setPointSizeF(size)
    font.setBold(bold)
    fmt = QgsTextFormat()
    fmt.setFont(font)
    fmt.setSize(size)
    item.setTextFormat(fmt)
    item.attemptMove(QgsLayoutPoint(x, y, MM))
    item.attemptResize(QgsLayoutSize(w, h, MM))
    layout.addLayoutItem(item)
    return item


NICE_SCALES = (500, 1000, 2000, 2500, 5000, 10000, 25000, 50000, 100000, 200000, 500000)


def nice_scale(scale):
    """Ближайший сверху стандартный масштаб: 1:4 518 → 1:5 000."""
    return next((s for s in NICE_SCALES if s >= scale), scale)


def _template_items(layout):
    """Рамка ГОСТ, форма 3 со штампом (тот же шаблон, что в «Листах вдоль трассы»)."""
    doc = QDomDocument()
    with open(TEMPLATE, encoding="utf-8") as f:
        doc.setContent(f.read())
    items, _ = layout.loadFromTemplate(doc, QgsReadWriteContext())
    return {i.id(): i for i in items if i.id()}, items


def _set_label(items, key, text, size=None):
    item = items.get(key)
    if not isinstance(item, QgsLayoutItemLabel):
        return
    item.setText(text)
    if size:
        fmt = item.textFormat()
        fmt.setSize(size)
        item.setTextFormat(fmt)


def style_terr_zones(layer, field="name_by_doc"):
    """Территориальные зоны: цвет по названию зоны (полупрозрачно), подпись — индекс зоны
    в скобках («Ж-1»), если он есть, иначе название."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    index = layer.fields().indexOf(field)
    if index < 0:
        _outline(layer, "120,60,160", 0.5, fill="200,170,230,90")
        return
    palette = ("230,159,0", "86,180,233", "0,158,115", "240,228,66", "0,114,178", "213,94,0",
               "204,121,167", "153,153,153", "117,112,179", "166,216,84")
    categories = []
    for n, value in enumerate(sorted(v for v in layer.uniqueValues(index) if v)):
        symbol = QgsFillSymbol.createSimple({"color": palette[n % len(palette)] + ",110",
                                             "outline_color": "80,80,80", "outline_width": "0.3"})
        categories.append(QgsRendererCategory(value, symbol, str(value)))
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
    settings = QgsPalLayerSettings()
    settings.isExpression = True
    settings.fieldName = ("coalesce(nullif(regexp_substr(\"{0}\", '\\\\(([^()]+)\\\\)\\\\s*$'), ''), \"{0}\")"
                          .format(field))
    text = QgsTextFormat()
    font = QFont()
    font.setPointSizeF(8)
    font.setBold(True)
    text.setFont(font)
    text.setSize(8)
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(0.8)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    settings.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def _theme(project, name, layers):
    """Тема карты name (прежняя заменяется): включены ровно эти слои — для переключения на экране."""
    from qgis.core import QgsMapThemeCollection
    record = QgsMapThemeCollection.MapThemeRecord()
    for layer in layers:
        record.addLayerRecord(QgsMapThemeCollection.MapThemeLayerRecord(layer))
    themes = project.mapThemeCollection()
    if themes.hasMapTheme(name):
        themes.update(name, record)
    else:
        themes.insert(name, record)


def _coord_table(layout, rows, header, reserve_top):
    """Каталог координат справа на поле карты: столбцы по высоте поля. Ширина занятой полосы, мм,
    или 0, если точек больше, чем помещается (тогда каталог — только в Word)."""
    from qgis.core import (QgsLayoutFrame, QgsLayoutItemManualTable, QgsLayoutTable,
                           QgsLayoutTableColumn, QgsTableCell)
    table = QgsLayoutItemManualTable.create(layout)
    table.setHeaders([QgsLayoutTableColumn(h) for h in ("№", "X, м", "Y, м")])
    table.setIncludeTableHeader(True)
    table.setHeaderMode(QgsLayoutTable.HeaderMode.AllFrames)
    table.setTableContents([[QgsTableCell(str(n)), QgsTableCell("{:.2f}".format(x)),
                             QgsTableCell("{:.2f}".format(y))] for n, x, y in rows])
    table.setColumnWidths([8, 24, 24])
    for setter, bold in ((table.setContentTextFormat, False), (table.setHeaderTextFormat, True)):
        font = QFont()
        font.setPointSizeF(7)
        font.setBold(bold)
        text = QgsTextFormat()
        text.setFont(font)
        text.setSize(7)
        setter(text)
    table.setBackgroundColor(QColor(255, 255, 255))
    layout.addMultiFrame(table)
    first = QgsLayoutFrame(layout, table)  # размер таблицы считается только с рамкой
    table.addFrame(first)
    size = table.totalSize()
    row = size.height()  # пустая рамка вмещает одну шапку: её высота = высота строки (шрифт тот же)
    top = FRAME + 10 + reserve_top  # ниже угловой графы рамки
    per_frame = int((STAMP_TOP - 3 - top) / row) - 1  # строка шапки — в каждом столбце
    frames = -(-len(rows) // max(per_frame, 1))
    width = size.width() + 3
    if frames > 3:
        layout.removeMultiFrame(table)
        layout.removeLayoutItem(first)
        return 0
    for k in range(frames):
        frame = first if k == 0 else QgsLayoutFrame(layout, table)
        frame.attemptMove(QgsLayoutPoint(PAGE_W - FRAME - (frames - k) * width, top, MM))
        frame.attemptResize(QgsLayoutSize(size.width(), (per_frame + 1) * row + 1, MM))
        frame.setBackgroundEnabled(True)
        if k:
            table.addFrame(frame)
    _label(layout, header, PAGE_W - FRAME - frames * width, FRAME + 9, frames * width - 3,
           reserve_top, 8, True)
    return frames * width + 3


def build_layout(project, title, subtitle, extent, legend_layers, crs=None, object_name="",
                 name=LAYOUT_NAME, hidden=(), rasters_only=False, table=None):
    """Новый макет name в рамке ГОСТ и одноимённая тема карты (прежние заменяются).
    legend_layers — [(слой, подпись в легенде или None — имя слоя)], сверху вниз. Набор слоёв карты
    закреплён: слои легенды, затем включённые в проекте, кроме hidden (rasters_only — только растры:
    подложка, без чужих векторных слоёв). table — (заголовок, [(№, X, Y)]): каталог справа на поле."""
    manager = project.layoutManager()
    old = manager.layoutByName(name)
    if old is not None:
        manager.removeLayout(old)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    named, loaded = _template_items(layout)
    layout.setName(name)
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, MM))
    for item in loaded:  # легенда шаблона — все слои проекта: ставим свою
        if isinstance(item, QgsLayoutItemLegend):
            layout.removeLayoutItem(item)

    # карта — на всё поле рамки; охват вписывается в часть над штампом (и левее каталога),
    # под штампом и за каталогом — продолжение карты
    item = next(i for i in loaded if isinstance(i, QgsLayoutItemMap))
    for grid in item.grids().asList():  # градусная сетка шаблона на схеме СЗЗ не нужна
        item.grids().removeGrid(grid.id())
    wanted = [l for l, _ in legend_layers if l is not None]
    shown = [l for l in project.layerTreeRoot().checkedLayers()
             if l not in hidden and l not in wanted
             and (not rasters_only or l.type() == QgsMapLayerType.RasterLayer)]
    item.setFollowVisibilityPreset(False)
    item.setLayers(wanted + shown)  # сверху вниз: границы и участки над подложкой
    item.setKeepLayerSet(True)
    _theme(project, name, wanted + shown)
    reserve = _coord_table(layout, table[1], table[0], 8) if table else 0
    if table and not reserve:
        subtitle += "; точек больше, чем помещается на листе, — каталог координат в реестре Word"
    item.setCrs(crs if crs is not None and crs.isValid() else project.crs())
    item.attemptMove(QgsLayoutPoint(FRAME_LEFT, FRAME, MM))
    item.attemptResize(QgsLayoutSize(FIELD_W - reserve, STAMP_TOP - FRAME, MM))
    item.zoomToExtent(extent)
    item.setScale(nice_scale(item.scale()))
    top = item.extent()
    unit = top.width() / (FIELD_W - reserve)
    item.attemptResize(QgsLayoutSize(FIELD_W, FIELD_H, MM))
    item.setExtent(QgsRectangle(top.xMinimum(), top.yMaximum() - FIELD_H * unit,
                                top.xMinimum() + FIELD_W * unit, top.yMaximum()))
    layout.moveItemToBottom(item)  # карта под рамкой, штампом и каталогом

    arrow = next((i for i in loaded if isinstance(i, QgsLayoutItemPicture)), None)
    if arrow is not None:
        arrow.setPicturePath(NORTH_ARROW)
        arrow.setLinkedMap(item)

    # слева от штампа: легенда, масштабная линейка, источник
    box = QgsLayoutItemShape(layout)
    box.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
    box.setSymbol(QgsFillSymbol.createSimple({"color": "255,255,255", "outline_color": "0,0,0",
                                              "outline_width": "0.5"}))
    box.attemptMove(QgsLayoutPoint(FRAME_LEFT, STAMP_TOP, MM))
    box.attemptResize(QgsLayoutSize(STAMP_LEFT - FRAME_LEFT, PAGE_H - FRAME - STAMP_TOP, MM))
    layout.addLayoutItem(box)

    legend = QgsLayoutItemLegend(layout)
    legend.setTitle("Условные обозначения")
    legend.setLinkedMap(item)
    legend.setAutoUpdateModel(False)
    legend.setBackgroundEnabled(False)
    root = legend.model().rootGroup()
    root.removeAllChildren()
    for layer, name in legend_layers:
        # пустой слой (например, в СЗЗ участков нет) — не в легенду: на карте его не видно
        if layer is None or (hasattr(layer, "featureCount") and layer.featureCount() == 0):
            continue
        node = root.addLayer(layer)
        if name:
            node.setUseLayerName(False)
            node.setName(name)
            node.setCustomProperty("legend/title-label", name)
            # у слоя с одним символом легенда пишет подпись символа, а не заголовок слоя
            QgsMapLayerLegendUtils.setLegendNodeUserLabel(node, 0, name)
            legend.model().refreshLayerLegend(node)
    legend.attemptMove(QgsLayoutPoint(FRAME_LEFT + 2, STAMP_TOP + 1, MM))
    layout.addLayoutItem(legend)
    legend.adjustBoxSize()

    side_x = STAMP_LEFT - SIDE
    scale = QgsLayoutItemScaleBar(layout)
    scale.setLinkedMap(item)
    scale.setStyle("Single Box")
    scale.applyDefaultSize(Qgis.DistanceUnit.Meters)
    # по умолчанию линейка до 150 мм — подпись правого конца заезжала на штамп:
    # два деления круглой длины, около 20 мм каждое
    step = item.scale() * 0.02  # метров в 20 мм
    nice = max(v * 10 ** k for k in range(-1, 7) for v in (1, 2, 2.5, 5) if v * 10 ** k <= step * 1.25)
    scale.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.Fixed)
    scale.setUnitsPerSegment(nice)
    scale.setNumberOfSegments(2)
    scale.setNumberOfSegmentsLeft(0)
    scale.setUnitLabel("м")
    scale.setBackgroundEnabled(False)
    scale.attemptMove(QgsLayoutPoint(side_x, STAMP_TOP + 3, MM))
    layout.addLayoutItem(scale)
    _label(layout, subtitle[:1].upper() + subtitle[1:] + ".\nСведения о границах участков — ЕГРН (публичная кадастровая карта), "
                   "справочно; составлено {}.".format(datetime.date.today().strftime("%d.%m.%Y")),
           side_x, STAMP_TOP + 20, SIDE - 3, 30, 7)

    scale_text = "Масштаб 1:{:,.0f}".format(item.scale()).replace(",", " ")
    _set_label(named, "3-наименование здания", title, 9)
    _set_label(named, "2-наименование комплекса", object_name)
    _set_label(named, "4-наимнование изображений", scale_text)
    for key in ("1-обозначение документа", "27-ЗАКАЗЧИК", "6-обозначение стадии"):
        _set_label(named, key, "")  # заполняется в макете
    _set_label(named, "7-номер листа", "1")
    _set_label(named, "8-кол-во листов", "1")
    manager.addLayout(layout)
    return layout


def export_pdf(layout, path):
    """True — PDF записан."""
    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    settings.dpi = 200
    return exporter.exportToPdf(path, settings) == QgsLayoutExporter.ExportResult.Success
