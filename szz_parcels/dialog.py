"""Окно «Реестр участков в СЗЗ»: выбор слоёв, вопрос о зонах, подсветка на карте."""

import datetime
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
from qgis.gui import (
    QgsCollapsibleGroupBox,
    QgsFieldComboBox,
    QgsFileWidget,
    QgsMapLayerComboBox,
    QgsRubberBand,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import core

SETTINGS = "szz_parcels/"
MIN_WIDTH = 560
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
    def __init__(self, iface, run_callback, nspd_callback=None, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.run_callback = run_callback
        self.nspd_callback = nspd_callback
        self.setWindowTitle("Реестр участков в СЗЗ")
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
        self._fit()

    # ------------------------------------------------------------ окно

    def _build(self):
        outer = QVBoxLayout(self)
        # содержимое — в прокрутке: на ноутбуке окно с вопросом о зонах не уходит за край экрана
        self.body = QWidget()
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        box = QGroupBox("Предприятие и СЗЗ")
        form = QFormLayout(box)
        self.enterprise = QgsMapLayerComboBox()
        self.enterprise.setFilters(_layer_filter("PolygonLayer", "LineLayer"))
        self.enterprise.setAllowEmptyLayer(True)  # не угадана — пусто, а не первый слой списка
        form.addRow("Граница предприятия:", self.enterprise)
        self.enterprise_selected = QCheckBox("только выделенные объекты")
        form.addRow("", self.enterprise_selected)
        self.same = QCheckBox("СЗЗ совпадает с границей предприятия")
        form.addRow("", self.same)
        self.szz = QgsMapLayerComboBox()
        self.szz.setFilters(_layer_filter("PolygonLayer"))
        # слой СЗЗ не угадывается: без слоя «СЗЗ…» в проекте выбор пустой, а не слой участков
        self.szz.setAllowEmptyLayer(True)
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
        self.zone_list.setMaximumHeight(110)
        zones.addWidget(self.zone_list)
        layout.addWidget(self.zones_box)

        box = QGroupBox("Земельные участки")
        form = QFormLayout(box)
        self.parcels = QgsMapLayerComboBox()
        self.parcels.setFilters(_layer_filter("PolygonLayer"))
        # без слоя с кадастровым номером — пусто, а не первый попавшийся полигон (СЗЗ, граница)
        self.parcels.setAllowEmptyLayer(True)
        self.nspd_button = QPushButton("Загрузить с НСПД")
        self.nspd_button.setToolTip(
            "Загрузить земельные участки с публичной кадастровой карты НСПД в зоне поиска: "
            "СЗЗ (или граница предприятия) плюс расстояние поиска. Нужен интернет из России.")
        self.nspd_button.clicked.connect(self._load_nspd)
        form.addRow("Слой участков:", self.parcels)
        # своей строкой: рядом со списком кнопка сжимала его, и имени слоя не было видно
        row = QHBoxLayout()
        row.addWidget(self.nspd_button)
        row.addStretch(1)
        form.addRow("", row)
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
        self.direction = QCheckBox("указывать направление от предприятия (румб)")
        self.direction.setChecked(_settings_value("direction", True, bool))
        self.direction_from = QComboBox()
        self.direction_from.addItems(["от центра предприятия", "от ближайшей точки границы"])
        self.direction_from.setCurrentIndex(_settings_value("direction_from", 0, int))
        self.direction_from.setEnabled(self.direction.isChecked())
        self.direction.toggled.connect(self.direction_from.setEnabled)
        self.direction.toggled.connect(self._remember)
        self.direction_from.currentIndexChanged.connect(self._remember)
        form.addRow("", self.direction)
        form.addRow("Направление считать:", self.direction_from)
        layout.addWidget(box)

        # допуски расхождения границ: свёрнуто, значения запоминаются
        self.advanced = QgsCollapsibleGroupBox("Дополнительно: допуски расхождения границ")
        self.advanced.setCollapsed(True)
        self.advanced.setSaveCollapsedState(False)
        form = QFormLayout(self.advanced)
        hint = QLabel("Заход в СЗЗ меньше этой площади или полосой уже допуска для категории "
                      "земель участка — расхождение границ, а не попадание в СЗЗ.")
        hint.setWordWrap(True)
        form.addRow(hint)
        self.min_area = self._spin(" м²", 0, 1000000, 1, "min_area", core.MIN_OVERLAP_M2)
        form.addRow("Заход меньше:", self.min_area)
        self.depth_spins = {}
        for key, name in core.DEPTH_NAMES.items():
            spin = self._spin(" м", 0, 1000, 2, "depth_" + key, core.DEPTHS[key])
            self.depth_spins[key] = spin
            form.addRow(name[:1].upper() + name[1:] + ":", spin)
        self.defaults_button = QPushButton("По умолчанию")
        self.defaults_button.setToolTip("Вернуть допуски по требованиям Росреестра к точности границ")
        self.defaults_button.clicked.connect(self._default_tolerances)
        form.addRow("", self.defaults_button)
        layout.addWidget(self.advanced)

        box = QGroupBox("Результат: два слоя в проекте и файлы")
        form = QFormLayout(box)
        self.object_name = QLineEdit()
        self.object_name.setPlaceholderText("например, ПС 110 кВ Южная")
        form.addRow("Предприятие (для Word):", self.object_name)
        self.xlsx_on, self.xlsx = self._file_row(form, "Excel:", "Excel (*.xlsx)", "xlsx")
        self.docx_on, self.docx = self._file_row(form, "Word:", "Word (*.docx)", "docx")
        self.map_on = QCheckBox("карта PDF (A3) и макет «Карта СЗЗ»")
        self.map_on.setToolTip("Карта с участками, СЗЗ и зоной поиска; подложка — как включена "
                               "в проекте. Макет можно поправить и вывести заново.")
        self.map_on.setChecked(_settings_value("map", True, bool))
        self.map_on.toggled.connect(self._remember)
        form.addRow("Карта:", self.map_on)
        layout.addWidget(box)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton("Сформировать",
                                            QDialogButtonBox.ButtonRole.AcceptRole)
        close = buttons.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        close.clicked.connect(self.close)
        self.run_button.clicked.connect(self._run)
        outer.addWidget(buttons)

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
        on.setToolTip("Сохранять таблицы в {}".format(label.rstrip(":")))
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
        """Слои прошлого расчёта в этом проекте, иначе — по названиям и полям: участки — с
        кадастровым номером, СЗЗ — «СЗЗ…», граница — «границ…»/«предприят…». Слои результата
        модуля («В границах СЗЗ», «За границей…») не подставляются никуда: у них есть и «границ»
        в названии, и поле cad_num."""
        project = QgsProject.instance()
        saved = {key: project.layerStore().mapLayer(project.readEntry("szz_parcels", key, "")[0])
                 for key in ("enterprise", "parcels", "szz")}
        layers = [l for l in project.mapLayers().values()
                  if isinstance(l, QgsVectorLayer) and not _is_result(l)]

        def first(test):
            return next((l for l in layers if test(l)), None)

        polygon = Qgis.GeometryType.Polygon
        self.parcels.setLayer(saved["parcels"] or first(
            lambda l: l.geometryType() == polygon and core.find_field(l.fields().names(), core.CAD_FIELDS)))
        self.szz.setLayer(saved["szz"] or first(
            lambda l: l.geometryType() == polygon and any(w in l.name().lower() for w in ("сзз", "санитарно"))))
        enterprise = saved["enterprise"] or first(
            lambda l: l.geometryType() in (polygon, Qgis.GeometryType.Line)
            and any(w in l.name().lower() for w in ("границ", "предприят"))
            and l is not self.parcels.currentLayer())
        self.enterprise.setLayer(enterprise)
        self.same.setChecked(_settings_value("same", False, bool))
        self._parcels_changed()

    def _spin(self, suffix, low, high, decimals, key, default):
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setValue(_settings_value(key, default, float))
        spin.setProperty("key", key)
        spin.valueChanged.connect(self._remember)
        spin.valueChanged.connect(self.schedule)
        return spin

    def _default_tolerances(self):
        self.min_area.setValue(core.MIN_OVERLAP_M2)
        for key, spin in self.depth_spins.items():
            spin.setValue(core.DEPTHS[key])

    def tolerances(self):
        """(порог площади, допуски по группам категорий) — как в окне."""
        return self.min_area.value(), {k: s.value() for k, s in self.depth_spins.items()}

    def _remember(self, *args):
        settings = QgsSettings()
        settings.setValue(SETTINGS + "distance", self.distance.value())
        settings.setValue(SETTINGS + "share", self.share.isChecked())
        settings.setValue(SETTINGS + "same", self.same.isChecked())
        settings.setValue(SETTINGS + "xlsx", self.xlsx_on.isChecked())
        settings.setValue(SETTINGS + "docx", self.docx_on.isChecked())
        for key in ("direction", "direction_from", "map_on"):
            widget = getattr(self, key, None)
            if widget is not None:
                value = widget.currentIndex() if key == "direction_from" else widget.isChecked()
                settings.setValue(SETTINGS + ("map" if key == "map_on" else key), value)
        for spin in [getattr(self, "min_area", None)] + list(getattr(self, "depth_spins", {}).values()):
            if spin is not None:
                settings.setValue(SETTINGS + spin.property("key"), spin.value())

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
        self.update_nspd_button()
        self._suggest_files()
        self.schedule()

    def _selection_changed(self, *args):
        self._update_selection_labels()
        self._fit()
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
        self._fit()

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

    def update_nspd_button(self):
        """Загрузка с НСПД нужна граница и СЗЗ (или «совпадает с границей»), слой участков — нет."""
        ready = self.enterprise.currentLayer() is not None and (
            self.same.isChecked() or self.szz.currentLayer() is not None)
        self.nspd_button.setEnabled(ready and self.nspd_callback is not None)

    def nspd_parameters(self):
        """Параметры szzparcels:nspd: та же зона, что у реестра, файл — в «Карты СЗЗ»."""
        params = self.parameters_zone()
        params["DISTANCE"] = self.distance.value()
        folder = os.path.join(QgsProject.instance().homePath() or os.path.expanduser("~"),
                              OUTPUT_FOLDER)
        stem = os.path.join(folder, "Участки НСПД {}".format(datetime.date.today().isoformat()))
        number = 1
        while os.path.exists(_numbered(stem + ".gpkg", number)):
            number += 1
        params["OUTPUT"] = _numbered(stem + ".gpkg", number)
        return params

    def _load_nspd(self):
        os.makedirs(os.path.dirname(self.nspd_parameters()["OUTPUT"]), exist_ok=True)
        self.nspd_callback(self.nspd_parameters(), self)

    def _map_path(self):
        """«Карта СЗЗ.pdf» — рядом с Excel (или в «Карты СЗЗ»), прежняя не затирается."""
        base = self.xlsx.filePath() or self.docx.filePath()
        folder = os.path.dirname(base) if base else os.path.join(
            QgsProject.instance().homePath() or os.path.expanduser("~"), OUTPUT_FOLDER)
        path = os.path.join(folder, "Карта СЗЗ.pdf")
        number = 1
        while os.path.exists(_numbered(path, number)):
            number += 1
        return _numbered(path, number)

    def _output_files(self):
        """Пути Excel и Word. Файл по умолчанию, который уже есть (прежний реестр), не
        затирается: новый получает номер « 2», « 3»… общий для обоих файлов.
        Путь, выбранный вручную, — как есть: при выборе QGIS уже спросил о замене."""
        chosen = {}
        for key, on, widget in (("XLSX", self.xlsx_on, self.xlsx), ("DOCX", self.docx_on, self.docx)):
            if on.isChecked() and widget.filePath():
                chosen[key] = (widget.filePath(), widget.filePath() == widget.property("default"))
        number = 1
        while any(auto and os.path.exists(_numbered(path, number))
                  for path, auto in chosen.values()):
            number += 1
        return {key: _numbered(path, number) if auto else path
                for key, (path, auto) in chosen.items()}

    def _fit(self):
        """Размер окна по содержимому: ширина — чтобы подписи не обрезались, высота — без
        пустого места, но не выше экрана (остальное — прокруткой)."""
        self.body.adjustSize()
        bar = self.scroll.verticalScrollBar().sizeHint().width()
        self.scroll.setMinimumWidth(self.body.minimumSizeHint().width() + bar)
        layout = self.layout()
        layout.activate()
        outside = self.sizeHint().height() - self.scroll.sizeHint().height()
        wanted = outside + self.body.sizeHint().height() + 2 * self.scroll.frameWidth()
        screen = self.screen() if hasattr(self, "screen") else None
        if screen is not None:
            wanted = min(wanted, screen.availableGeometry().height() - 60)
        width = max(MIN_WIDTH, self.minimumSizeHint().width(), self.width() if self.isVisible() else 0)
        self.resize(width, wanted)

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
            return "Выберите слой земельных участков или загрузите их кнопкой «Загрузить с НСПД»."
        if not self.cad_field.currentField():
            return "Выберите поле кадастрового номера."
        if not self.same.isChecked():
            if self.szz.currentLayer() is None:
                return "Выберите слой СЗЗ или отметьте «СЗЗ совпадает с границей предприятия»."
            mode, ids = self.zone_choice()
            if mode == LIST and not ids:
                return "Отметьте в списке хотя бы одну зону."
        return None

    def parameters_zone(self):
        """Граница предприятия и выбранные зоны СЗЗ — общие для реестра и загрузки с НСПД."""
        enterprise = self.enterprise.currentLayer()
        params = {"ENTERPRISE": QgsProcessingFeatureSourceDefinition(
            enterprise.id(), self.enterprise_selected.isChecked())}
        if not self.same.isChecked():
            szz = self.szz.currentLayer()
            mode, ids = self.zone_choice()
            source = QgsProcessingFeatureSourceDefinition(szz.id(), mode == SELECTED)
            if mode == LIST:
                source.filterExpression = "$id IN ({})".format(", ".join(str(i) for i in ids))
            params["SZZ"] = source
        return params

    def parameters(self):
        """Параметры алгоритма szzparcels:registry по выбору в окне."""
        params = {
            "PARCELS": self.parcels.currentLayer().id(),
            "CAD_FIELD": self.cad_field.currentField(),
            "CATEGORY_FIELD": self.category_field.currentField() or None,
            "VRI_DOC_FIELD": self.vri_field.currentField() or None,
            "DISTANCE": self.distance.value(),
            "SHARE": self.share.isChecked(),
            "MIN_OVERLAP": self.min_area.value(),
            "DIRECTION": self.direction.isChecked(),
            "DIRECTION_FROM": self.direction_from.currentIndex(),
            "SEARCH": "TEMPORARY_OUTPUT",
            "MAP_PDF": self._map_path() if self.map_on.isChecked() else None,
            "OBJECT": self.object_name.text().strip(),
            "INSIDE": "TEMPORARY_OUTPUT",
            "NEAR": "TEMPORARY_OUTPUT",
            "XLSX": None,
            "DOCX": None,
        }
        params.update(self._output_files())
        params.update(self.parameters_zone())
        from .processing.registry_algorithm import SzzRegistryAlgorithm
        for key, spin in self.depth_spins.items():
            params[SzzRegistryAlgorithm.DEPTH_PARAMS[key]] = spin.value()
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
            # без жёсткого цвета: тёмно-красный не читается в тёмной теме QGIS
            self.status.setText("⚠ <b>{}</b>".format(self.error))
        # строка итога выросла или сжалась (1–3 строки) — подогнать окно, иначе лишняя прокрутка
        height = self.status.heightForWidth(self.status.width()) if self.status.width() else 0
        if height != getattr(self, "_status_height", None):
            self._status_height = height
            self._fit()

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
            raise ValueError("Граница предприятия не замкнута: соедините концы линии в чертеже "
                             "или нарисуйте границу полигоном.")
        if enterprise is None:
            raise ValueError("В границе предприятия нет контуров — выберите другой слой или "
                             "снимите «только выделенные объекты».")
        zone = None
        if not self.same.isChecked():
            szz = self.szz.currentLayer()
            zone = core.area_geometry(
                self._geometries(self._zone_features(szz), szz.crs(), work_crs))
            if zone is None:
                raise ValueError("В выбранных зонах СЗЗ нет контуров — отметьте другую зону "
                                 "или выберите «все зоны слоя».")
        min_area, depths = self.tolerances()
        classifier = core.Classifier(enterprise, zone, self.distance.value(),
                                     min_overlap=min_area, depths=depths)
        search = classifier.search_area()
        request = core.parcel_request(search, work_crs, parcels.crs(),
                                      QgsProject.instance().transformContext())
        to_work = None
        if parcels.crs() != work_crs:
            to_work = QgsCoordinateTransform(parcels.crs(), work_crs, QgsProject.instance())
        rows, _ = core.collect(parcels.getFeatures(request), self.cad_field.currentField(),
                               to_work, classifier, None, 0,
                               self.category_field.currentField() or None,
                               self.vri_field.currentField() or None)
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
        # выбор слоёв — в проект: при следующем открытии окна те же слои, а не угаданные
        project = QgsProject.instance()
        for key, combo in (("enterprise", self.enterprise), ("parcels", self.parcels), ("szz", self.szz)):
            layer = combo.currentLayer()
            project.writeEntry("szz_parcels", key, layer.id() if layer else "")
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


def _numbered(path, number):
    """«Реестр.xlsx» → «Реестр 2.xlsx»; номер 1 — путь как есть. Без скобок: они ломают
    ссылки на файл (правило пользователя)."""
    if number <= 1:
        return path
    stem, ext = os.path.splitext(path)
    return "{} {}{}".format(stem, number, ext)


RESULT_NAMES = ("в границах сзз", "за границей сзз", "за границей предприятия")


def _is_result(layer):
    """Слой результата этого модуля (метка или название прошлого реестра)."""
    if layer.customProperty("szz_parcels/result", False):
        return True
    return layer.name().lower().startswith(RESULT_NAMES)
