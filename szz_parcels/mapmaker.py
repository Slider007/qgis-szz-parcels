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
TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "forma_3.qpt")
PAGE_W, PAGE_H = 420.0, 297.0  # A3 альбомный, мм
FRAME_LEFT, FRAME = 20.0, 5.0  # поля рамки ГОСТ 2.301
FIELD_W, FIELD_H = PAGE_W - FRAME_LEFT - FRAME, PAGE_H - 2 * FRAME
STAMP_LEFT, STAMP_TOP = 230.0, 237.0  # основная надпись 185×55 мм в правом нижнем углу
SIDE = 70.0  # колонка линейки и примечания у штампа
MM = Qgis.LayoutUnit.Millimeters
NORTH_ARROW = ":/images/north_arrows/layout_default_north_arrow.svg"


def style_search_zone(layer):
    """Зона поиска — синий пунктир без заливки, как подсветка в окне."""
    symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0", "outline_color": "31,119,180", "outline_style": "dash",
        "outline_width": "0.6"})
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


def build_layout(project, title, subtitle, extent, legend_layers, crs=None, object_name=""):
    """Новый макет «Карта СЗЗ» в рамке ГОСТ (прежний с тем же именем заменяется).
    legend_layers — [(слой, подпись в легенде или None — имя слоя)]."""
    manager = project.layoutManager()
    old = manager.layoutByName(LAYOUT_NAME)
    if old is not None:
        manager.removeLayout(old)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    named, loaded = _template_items(layout)
    layout.setName(LAYOUT_NAME)
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, MM))
    for item in loaded:  # легенда шаблона — все слои проекта: ставим свою
        if isinstance(item, QgsLayoutItemLegend):
            layout.removeLayoutItem(item)

    # карта — на всё поле рамки; охват вписывается в часть над штампом, под штампом — продолжение карты
    item = next(i for i in loaded if isinstance(i, QgsLayoutItemMap))
    for grid in item.grids().asList():  # градусная сетка шаблона на схеме СЗЗ не нужна
        item.grids().removeGrid(grid.id())
    item.setFollowVisibilityPreset(False)
    item.setKeepLayerSet(False)
    item.setCrs(crs if crs is not None and crs.isValid() else project.crs())
    item.attemptMove(QgsLayoutPoint(FRAME_LEFT, FRAME, MM))
    item.attemptResize(QgsLayoutSize(FIELD_W, STAMP_TOP - FRAME, MM))
    item.zoomToExtent(extent)
    item.setScale(nice_scale(item.scale()))
    top = item.extent()
    unit = top.width() / FIELD_W
    item.attemptResize(QgsLayoutSize(FIELD_W, FIELD_H, MM))
    item.setExtent(QgsRectangle(top.xMinimum(), top.yMaximum() - FIELD_H * unit,
                                top.xMinimum() + FIELD_W * unit, top.yMaximum()))
    layout.moveItemToBottom(item)  # карта под рамкой и штампом

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
