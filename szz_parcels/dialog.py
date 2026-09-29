"""Окно «Реестр участков в СЗЗ»: выбор слоёв, вопрос о зонах, подсветка на карте."""

import os

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsFieldProxyModel,
    QgsGeometry,
    QgsProcessingFeatureSourceDefinition,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.gui import QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
    QVBoxLayout,
)

from . import core

SETTINGS = "szz_parcels/"
OUTPUT_FOLDER = "Карты СЗЗ"
ALL, SELECTED, LIST = 0, 1, 2
INSIDE_COLOR = QColor(214, 39, 40)
NEAR_COLOR = QColor(255, 140, 0)
SEARCH_COLOR = QColor(31, 119, 180)


def _settings_value(key, default, kind):
    try:
        return QgsSettings().value(SETTINGS + key, default, type=kind)
    except TypeError:
        return default


def _layer_filter(*names):
    """Qgis.LayerFilters из имён. «|» в PyQt5 даёт int, а setFilters(int) устарел —
    отсюда обёртка Qgis.LayerFilters."""
    result = Qgis.LayerFilters()
    for name in names:
        result = Qgis.LayerFilters(result | getattr(Qgis.LayerFilter, name))
    return result


class SzzDialog(QDialog):
    def __init__(self, iface, run_callback, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.run_callback = run_callback
        self.setWindowTitle("Реестр участков в СЗЗ")
        self.setMinimumWidth(560)
        self._connected = []  # (слой, сигнал) — отключаются при смене слоя и закрытии
        self._bands = []
        self.error = None
        self.counts = (0, 0, 0)
        self._many_zones = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.update_preview)
        self._build()
        self._pick_defaults()
        self._layers_changed()

    # ------------------------------------------------------------ окно

    def _build(self):
        layout = QVBoxLayout(self)

        box = QGroupBox("Предприятие и СЗЗ")
        form = QFormLayout(box)
        self.enterprise = QgsMapLayerComboBox()
        self.enterprise.setFilters(_layer_filter("PolygonLayer", "LineLayer"))
        form.addRow("Граница предприятия:", self.enterprise)
        self.enterprise_selected = QCheckBox("только выделенные объекты")
        form.addRow("", self.enterprise_selected)
        self.same = QCheckBox("СЗЗ совпадает с границей предприятия")
        form.addRow("", self.same)
        self.szz = QgsMapLayerComboBox()
        self.szz.setFilters(_layer_filter("PolygonLayer"))
        self.szz_label = QLabel("Слой СЗЗ:")
        form.addRow(self.szz_label, self.szz)
        layout.addWidget(box)

        self.zones_box = QGroupBox("В слое СЗЗ несколько зон — какие взять?")
        zones = QVBoxLayout(self.zones_box)
        self.zone_mode = QButtonGroup(self)
        self.zone_all = QRadioButton()
        self.zone_selected = QRadioButton()
        self.zone_list_radio = QRadioButton("отмеченные в списке:")
        for mode, button in ((ALL, self.zone_all), (SELECTED, self.zone_selected),
                             (LIST, self.zone_list_radio)):
            self.zone_mode.addButton(button, mode)
            zones.addWidget(button)
        self.zone_list = QListWidget()
        self.zone_list.setMaximumHeight(130)
        zones.addWidget(self.zone_list)
        layout.addWidget(self.zones_box)

        box = QGroupBox("Земельные участки")
        form = QFormLayout(box)
        self.parcels = QgsMapLayerComboBox()
        self.parcels.setFilters(_layer_filter("PolygonLayer"))
        form.addRow("Слой участков:", self.parcels)
        self.cad_field = QgsFieldComboBox()
        self.category_field = QgsFieldComboBox()
        self.vri_field = QgsFieldComboBox()
        for combo in (self.category_field, self.vri_field):
            combo.setAllowEmptyFieldName(True)
        self.cad_field.setFilters(QgsFieldProxyModel.Filter.String)
        form.addRow("Кадастровый номер:", self.cad_field)
        form.addRow("Категория земель:", self.category_field)
        form.addRow("ВРИ по документу:", self.vri_field)
        self.distance = QDoubleSpinBox()
        self.distance.setRange(0, 100000)
        self.distance.setDecimals(0)
        self.distance.setSingleStep(10)
        self.distance.setSuffix(" м")
        self.distance.setValue(_settings_value("distance", 100.0, float))
        form.addRow("Искать за границей СЗЗ до:", self.distance)
        self.share = QCheckBox("указывать долю участка в СЗЗ, %")
        self.share.setChecked(_settings_value("share", True, bool))
        form.addRow("", self.share)
        layout.addWidget(box)

        box = QGroupBox("Результат: два слоя в проекте и файлы")
        form = QFormLayout(box)
        self.object_name = QLineEdit()
        self.object_name.setPlaceholderText("например, ПС 110 кВ Южная")
        form.addRow("Предприятие (для Word):", self.object_name)
        self.xlsx_on, self.xlsx = self._file_row(form, "Excel:", "Excel (*.xlsx)", "xlsx")
        self.docx_on, self.docx = self._file_row(form, "Word:", "Word (*.docx)", "docx")
        layout.addWidget(box)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton("Сформировать",
                                            QDialogButtonBox.ButtonRole.AcceptRole)
        close = buttons.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        close.clicked.connect(self.close)
        self.run_button.clicked.connect(self._run)
        layout.addWidget(buttons)

        self.enterprise.layerChanged.connect(self._layers_changed)
        self.szz.layerChanged.connect(self._layers_changed)
        self.parcels.layerChanged.connect(self._parcels_changed)
        self.same.toggled.connect(self._layers_changed)
        self.enterprise_selected.toggled.connect(self.schedule)
        self.zone_mode.buttonToggled.connect(self._zone_mode_changed)
        self.zone_list.itemChanged.connect(self.schedule)
        self.distance.valueChanged.connect(self.schedule)
        for combo in (self.cad_field, self.category_field, self.vri_field):
            combo.fieldChanged.connect(self.schedule)
        self.share.toggled.connect(self._remember)

    def _file_row(self, form, label, file_filter, suffix):
        on = QCheckBox()
        on.setChecked(_settings_value(suffix, True, bool))
        widget = QgsFileWidget()
        widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        widget.setFilter(file_filter)
        widget.setConfirmOverwrite(True)
        row = QHBoxLayout()
        row.addWidget(on)
        row.addWidget(widget, 1)
        form.addRow(label, row)
        on.toggled.connect(widget.setEnabled)
        on.toggled.connect(self._remember)
        widget.setEnabled(on.isChecked())
        widget.setProperty("suffix", suffix)
        return on, widget

    def _pick_defaults(self):
        """Слои по названиям и полям: участки — с кадастровым номером, СЗЗ — «СЗЗ…»."""
        layers = [l for l in QgsProject.instance().mapLayers().values()
                  if isinstance(l, QgsVectorLayer)]
        for layer in layers:
            if layer.geometryType() == Qgis.GeometryType.Polygon and \
                    core.find_field(layer.fields().names(), core.CAD_FIELDS):
                self.parcels.setLayer(layer)
                break
        for layer in layers:
            name = layer.name().lower()
            if layer.geometryType() == Qgis.GeometryType.Polygon and \
                    ("сзз" in name or "санитарно" in name):
                self.szz.setLayer(layer)
                break
        for layer in layers:
            if layer.geometryType() in (Qgis.GeometryType.Polygon, Qgis.GeometryType.Line) and \
                    ("границ" in layer.name().lower() or "предприят" in layer.name().lower()) \
                    and layer is not self.parcels.currentLayer():
                self.enterprise.setLayer(layer)
                break
        self.same.setChecked(_settings_value("same", False, bool))
        self._parcels_changed()

    def _remember(self, *args):
        settings = QgsSettings()
        settings.setValue(SETTINGS + "distance", self.distance.value())
        settings.setValue(SETTINGS + "share", self.share.isChecked())
        settings.setValue(SETTINGS + "same", self.same.isChecked())
        settings.setValue(SETTINGS + "xlsx", self.xlsx_on.isChecked())
        settings.setValue(SETTINGS + "docx", self.docx_on.isChecked())

    # ------------------------------------------------------------ слои

    def _disconnect_layers(self):
        for layer, signal, slot in self._connected:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connected = []

    def _watch(self, layer):
        if layer is None:
            return
        layer.selectionChanged.connect(self._selection_changed)
        self._connected.append((layer, layer.selectionChanged, self._selection_changed))

    def _layers_changed(self, *args):
        self._disconnect_layers()
        enterprise = self.enterprise.currentLayer()
        szz = None if self.same.isChecked() else self.szz.currentLayer()
        self._watch(enterprise)
        if szz is not None and szz is not enterprise:
            self._watch(szz)
        self.szz.setEnabled(not self.same.isChecked())
        self.szz_label.setEnabled(not self.same.isChecked())
        self._fill_zones()
        self._update_selection_labels()
        self._suggest_files()
        self.schedule()

    def _selection_changed(self, *args):
        self._update_selection_labels()
        self.schedule()

    def _update_selection_labels(self):
        enterprise = self.enterprise.currentLayer()
        count = enterprise.selectedFeatureCount() if enterprise else 0
        self.enterprise_selected.setText("только выделенные объекты ({})".format(count))
        self.enterprise_selected.setEnabled(count > 0)
        if count == 0:
            self.enterprise_selected.setChecked(False)
        szz = self.szz.currentLayer()
        total = szz.featureCount() if szz else 0
        selected = szz.selectedFeatureCount() if szz else 0
        self.zone_all.setText("все зоны слоя, объединить ({})".format(total))
        self.zone_selected.setText("выделенные на карте ({})".format(selected))
        self.zone_selected.setEnabled(selected > 0)
        if selected == 0 and self.zone_mode.checkedId() == SELECTED:
            self.zone_all.setChecked(True)

    def _zone_label_expression(self, layer):
        field = core.find_field(layer.fields().names(), core.ZONE_NAME_FIELDS)
        if field:
            return QgsExpression.quotedColumnRef(field)
        return layer.displayExpression() or "$id"

    def _fill_zones(self):
        szz = self.szz.currentLayer()
        many = (not self.same.isChecked() and szz is not None and szz.featureCount() > 1)
        self._many_zones = many
        self.zones_box.setVisible(many)
        self.zone_list.blockSignals(True)
        self.zone_list.clear()
        if many:
            expression = QgsExpression(self._zone_label_expression(szz))
            context = QgsExpressionContext(
                QgsExpressionContextUtils.globalProjectLayerScopes(szz))
            for n, feature in enumerate(szz.getFeatures(), 1):
                context.setFeature(feature)
                label = expression.evaluate(context)
                label = str(label) if label not in (None, "") else "объект {}".format(feature.id())
                item = QListWidgetItem("{}. {}".format(n, label[:150]))
                item.setToolTip(label)
                item.setData(Qt.ItemDataRole.UserRole, feature.id())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.zone_list.addItem(item)
            if szz.selectedFeatureCount() > 0:
                self.zone_selected.setChecked(True)
            elif self.zone_mode.checkedId() < 0:
                self.zone_all.setChecked(True)
        self.zone_list.blockSignals(False)
        self.zone_list.setEnabled(self.zone_mode.checkedId() == LIST)
        # вопрос о зонах появился или исчез — окно по содержимому, без пустого места
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())

    def _zone_mode_changed(self, *args):
        self.zone_list.setEnabled(self.zone_mode.checkedId() == LIST)
        self.schedule()

    def _parcels_changed(self, *args):
        layer = self.parcels.currentLayer()
        names = layer.fields().names() if layer else []
        for combo, candidates in ((self.cad_field, core.CAD_FIELDS),
                                  (self.category_field, core.CATEGORY_FIELDS),
                                  (self.vri_field, core.VRI_DOC_FIELDS)):
            combo.blockSignals(True)
            combo.setLayer(layer)
            combo.setField(core.find_field(names, candidates) or "")
            combo.blockSignals(False)
        self.schedule()

    def _suggest_files(self):
        """Файлы по умолчанию — в подпапке «Карты СЗЗ» рядом с проектом (проект не
        сохранён — в домашней папке). Путь, выбранный вручную, не трогается."""
        folder = os.path.join(QgsProject.instance().homePath() or os.path.expanduser("~"),
                              OUTPUT_FOLDER)
        for widget in (self.xlsx, self.docx):
            current = widget.filePath()
            if current and current != widget.property("default"):
                continue
            path = os.path.join(folder, "Реестр участков в СЗЗ." + widget.property("suffix"))
            widget.setProperty("default", path)
            widget.setFilePath(path)

    # ------------------------------------------------------------ выбор → расчёт

    def zone_choice(self):
        """(режим, fid отмеченных зон) — для слоя СЗЗ с несколькими зонами."""
        if not self._many_zones:
            return ALL, []
        ids = []
        for i in range(self.zone_list.count()):
            item = self.zone_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        mode = self.zone_mode.checkedId()
        return (ALL if mode < 0 else mode), ids

    def _zone_features(self, layer):
        mode, ids = self.zone_choice()
        if mode == SELECTED:
            return layer.getSelectedFeatures()
        if mode == LIST:
            return layer.getFeatures(QgsFeatureRequest().setFilterFids(ids))
        return layer.getFeatures()

    def check(self):
        """Текст ошибки выбора или None."""
        if self.enterprise.currentLayer() is None:
            return "Выберите слой границы предприятия."
        if self.parcels.currentLayer() is None:
            return "Выберите слой земельных участков."
        if not self.cad_field.currentField():
            return "Выберите поле кадастрового номера."
        if not self.same.isChecked():
            if self.szz.currentLayer() is None:
                return "Выберите слой СЗЗ или отметьте «СЗЗ совпадает с границей предприятия»."
            mode, ids = self.zone_choice()
            if mode == LIST and not ids:
                return "Отметьте в списке хотя бы одну зону."
        return None

    def parameters(self):
        """Параметры алгоритма szzparcels:registry по выбору в окне."""
        enterprise = self.enterprise.currentLayer()
        params = {
            "PARCELS": self.parcels.currentLayer().id(),
            "CAD_FIELD": self.cad_field.currentField(),
            "CATEGORY_FIELD": self.category_field.currentField() or None,
            "VRI_DOC_FIELD": self.vri_field.currentField() or None,
            "ENTERPRISE": QgsProcessingFeatureSourceDefinition(
                enterprise.id(), self.enterprise_selected.isChecked()),
            "DISTANCE": self.distance.value(),
            "SHARE": self.share.isChecked(),
            "OBJECT": self.object_name.text().strip(),
            "INSIDE": "TEMPORARY_OUTPUT",
            "NEAR": "TEMPORARY_OUTPUT",
            "XLSX": self.xlsx.filePath() if self.xlsx_on.isChecked() else None,
            "DOCX": self.docx.filePath() if self.docx_on.isChecked() else None,
        }
        if not self.same.isChecked():
            szz = self.szz.currentLayer()
            mode, ids = self.zone_choice()
            source = QgsProcessingFeatureSourceDefinition(szz.id(), mode == SELECTED)
            if mode == LIST:
                source.filterExpression = "$id IN ({})".format(", ".join(str(i) for i in ids))
            params["SZZ"] = source
        return params

    # ------------------------------------------------------------ подсветка

    def schedule(self, *args):
        self._timer.start()

    def _clear_bands(self):
        canvas = self.iface.mapCanvas()
        for band in self._bands:
            band.reset(Qgis.GeometryType.Polygon)
            if canvas.scene() is not None:
                canvas.scene().removeItem(band)
        self._bands = []

    def _band(self, color, fill, width, dashed=False):
        band = QgsRubberBand(self.iface.mapCanvas(), Qgis.GeometryType.Polygon)
        band.setStrokeColor(color)
        fill_color = QColor(color)
        fill_color.setAlpha(fill)
        band.setFillColor(fill_color)
        band.setWidth(width)
        if dashed:
            band.setLineStyle(Qt.PenStyle.DashLine)
        self._bands.append(band)
        return band

    def update_preview(self):
        """Пересчитать выборку и подсветить её на карте."""
        self._clear_bands()
        self.error = self.check()
        if self.error is None:
            try:
                self._preview()
            except ValueError as e:
                self.error = str(e)
        self.run_button.setEnabled(self.error is None)
        if self.error:
            self.status.setText('<span style="color:#b00">{}</span>'.format(self.error))

    def _geometries(self, features, layer_crs, work_crs):
        transform = None
        if layer_crs != work_crs:
            transform = QgsCoordinateTransform(layer_crs, work_crs, QgsProject.instance())
        result = []
        for feature in features:
            geometry = feature.geometry()
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                continue
            geometry = QgsGeometry(geometry)
            if transform is not None:
                geometry.transform(transform)
            result.append(geometry)
        return result

    def _preview(self):
        enterprise_layer = self.enterprise.currentLayer()
        parcels = self.parcels.currentLayer()
        ent_features = list(enterprise_layer.getSelectedFeatures()
                            if self.enterprise_selected.isChecked()
                            else enterprise_layer.getFeatures())
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        rough = self._geometries(ent_features, enterprise_layer.crs(), wgs)
        work_crs = core.working_crs(parcels.crs(), QgsGeometry.unaryUnion(rough) if rough else None)
        try:
            enterprise = core.area_geometry(
                self._geometries(ent_features, enterprise_layer.crs(), work_crs))
        except ValueError:
            raise ValueError("Граница предприятия: среди линий есть незамкнутая.")
        if enterprise is None:
            raise ValueError("В границе предприятия нет ни одного контура.")
        zone = None
        if not self.same.isChecked():
            szz = self.szz.currentLayer()
            zone = core.area_geometry(
                self._geometries(self._zone_features(szz), szz.crs(), work_crs))
            if zone is None:
                raise ValueError("В выбранных зонах СЗЗ нет ни одного контура.")
        classifier = core.Classifier(enterprise, zone, self.distance.value())
        search = classifier.search_area()
        request = core.parcel_request(search, work_crs, parcels.crs(),
                                      QgsProject.instance().transformContext())
        to_work = None
        if parcels.crs() != work_crs:
            to_work = QgsCoordinateTransform(parcels.crs(), work_crs, QgsProject.instance())
        rows, _ = core.collect(parcels.getFeatures(request), self.cad_field.currentField(),
                               to_work, classifier)
        inside, near, own = core.split(rows, classifier)
        self.counts = (len(inside), len(near), len(own))

        search_band = self._band(SEARCH_COLOR, 0, 2, dashed=True)
        search_band.setToGeometry(search, work_crs)
        zone_band = self._band(QColor(120, 0, 0), 0, 2)
        zone_band.setToGeometry(classifier.zone, work_crs)
        for items, color in ((inside, INSIDE_COLOR), (near, NEAR_COLOR)):
            geometries = [g for row, _ in items for g in row["geoms"]]
            if geometries:
                band = self._band(color, 90, 1)
                band.setToGeometry(QgsGeometry.collectGeometry(geometries), parcels.crs())
        where = "предприятия" if self.same.isChecked() else "СЗЗ"
        self.status.setText(
            "На карте: <b style='color:#d62728'>■</b> в границах СЗЗ — {}, "
            "<b style='color:#ff8c00'>■</b> за границей {} до {:g} м — {}; синий пунктир — "
            "граница поиска. Участков самого предприятия (в таблицы не входят): {}."
            .format(len(inside), where, self.distance.value(), len(near), len(own)))

    # ------------------------------------------------------------ запуск и закрытие

    def _run(self):
        self.update_preview()
        if self.error:
            return
        self._remember()
        self.run_callback(self.parameters(), self)

    def showEvent(self, event):
        super().showEvent(event)
        self._layers_changed()  # подключить сигналы слоёв и подсветить заново

    def hideEvent(self, event):
        # и «Закрыть», и Esc, и крестик окна: подсветка не должна остаться на карте
        self.cleanup()
        self._remember()
        super().hideEvent(event)

    def cleanup(self):
        """Для unload(): убрать подсветку и отключить сигналы слоёв."""
        self._timer.stop()
        self._clear_bands()
        self._disconnect_layers()
