"""Карта СЗЗ: макет A3 альбомный «Карта СЗЗ» в проекте и PDF.

Карта показывает слои проекта как они включены (подложка «как в проекте»), охват — зона
поиска с полями. Легенда — только слои реестра: граница предприятия, СЗЗ, участки в СЗЗ и
рядом, зона поиска. Объекты QGIS держатся в переменных: цепочки вида
layer.labeling().settings().format() в 3.44 роняют QGIS (временный объект удаляется раньше).
"""

import datetime

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
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPalLayerSettings,
    QgsPrintLayout,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont

LAYOUT_NAME = "Карта СЗЗ"
PAGE_W, PAGE_H = 420.0, 297.0  # A3 альбомный, мм
MARGIN = 10.0
SIDE = 95.0  # колонка легенды справа
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


def build_layout(project, title, subtitle, extent, legend_layers, crs=None):
    """legend_layers — [(слой, подпись в легенде или None — имя слоя)]."""
    """Новый макет «Карта СЗЗ» (прежний с тем же именем заменяется)."""
    manager = project.layoutManager()
    old = manager.layoutByName(LAYOUT_NAME)
    if old is not None:
        manager.removeLayout(old)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(LAYOUT_NAME)
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, MM))

    _label(layout, title, MARGIN, MARGIN - 2, PAGE_W - 2 * MARGIN, 9, 14, bold=True)
    _label(layout, subtitle, MARGIN, MARGIN + 7, PAGE_W - 2 * MARGIN, 6, 9)

    top = MARGIN + 15
    map_w = PAGE_W - 2 * MARGIN - SIDE - 5
    map_h = PAGE_H - top - MARGIN
    item = QgsLayoutItemMap(layout)
    item.attemptMove(QgsLayoutPoint(MARGIN, top, MM))
    item.attemptResize(QgsLayoutSize(map_w, map_h, MM))
    item.setFrameEnabled(True)
    item.setCrs(crs if crs is not None and crs.isValid() else project.crs())
    layout.addLayoutItem(item)
    item.zoomToExtent(extent)
    item.setScale(nice_scale(item.scale()))

    side_x = MARGIN + map_w + 5
    arrow = QgsLayoutItemPicture(layout)
    arrow.setPicturePath(NORTH_ARROW)
    arrow.attemptMove(QgsLayoutPoint(side_x + SIDE - 18, top, MM))
    arrow.attemptResize(QgsLayoutSize(15, 20, MM))
    arrow.setLinkedMap(item)
    layout.addLayoutItem(arrow)

    legend = QgsLayoutItemLegend(layout)
    legend.setTitle("Условные обозначения")
    legend.setLinkedMap(item)
    legend.setAutoUpdateModel(False)
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
    legend.attemptMove(QgsLayoutPoint(side_x, top + 24, MM))
    layout.addLayoutItem(legend)
    legend.adjustBoxSize()

    scale = QgsLayoutItemScaleBar(layout)
    scale.setLinkedMap(item)
    scale.setStyle("Single Box")
    scale.applyDefaultSize(Qgis.DistanceUnit.Meters)
    scale.setUnitLabel("м")
    scale.attemptMove(QgsLayoutPoint(side_x, PAGE_H - MARGIN - 32, MM))
    layout.addLayoutItem(scale)

    _label(layout, "Масштаб 1:{:,.0f}".format(item.scale()).replace(",", " "),
           side_x, PAGE_H - MARGIN - 18, SIDE, 5, 9)
    _label(layout, "Сведения о границах участков — ЕГРН (публичная кадастровая карта), "
                   "справочно; составлено {}.".format(datetime.date.today().strftime("%d.%m.%Y")),
           side_x, PAGE_H - MARGIN - 12, SIDE, 12, 7)
    manager.addLayout(layout)
    return layout


def export_pdf(layout, path):
    """True — PDF записан."""
    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    settings.dpi = 200
    return exporter.exportToPdf(path, settings) == QgsLayoutExporter.ExportResult.Success
