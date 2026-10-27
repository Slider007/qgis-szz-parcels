import os

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsProcessingAlgRunnerTask,
    QgsProcessingFeatureSourceDefinition,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QPushButton

from . import altan_toolbar, mapmaker, msk

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
ALGORITHM = "szzparcels:registry"
NSPD_ALGORITHM = "szzparcels:nspd"
ENTERPRISE_ALGORITHM = "szzparcels:nspd_parcel"
NSPD_STYLE = os.path.join(PLUGIN_DIR, "styles", "nspd_parcels.qml")
TITLE = "Участки в СЗЗ"
GROUP = "Реестр участков в СЗЗ"


class _Feedback(QgsProcessingFeedback):
    """Собирает предупреждения и ошибки алгоритма для сообщения пользователю."""

    def __init__(self):
        super().__init__()
        self.warnings = []
        self.errors = []

    def pushWarning(self, text):
        self.warnings.append(text)
        super().pushWarning(text)

    def reportError(self, text, fatalError=False):
        self.errors.append(text)
        super().reportError(text, fatalError)


class SzzParcelsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.provider = None
        self.dialog = None
        self._running = None  # (задача, контекст, feedback, параметры) — пока идёт расчёт

    def initProcessing(self):
        # QGIS сам вызывает initProcessing() у модулей с hasProcessingProvider=yes,
        # и initGui() — ещё раз: второй провайдер остался бы после выгрузки
        if self.provider is not None:
            return
        from .processing.provider import SzzParcelsProvider

        self.provider = SzzParcelsProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self.action = QAction(QIcon(os.path.join(PLUGIN_DIR, "icon.svg")),
                              "Реестр участков в СЗЗ", self.iface.mainWindow())
        self.action.setToolTip(
            "Земельные участки в границах санитарно-защитной зоны и ближайшие за ней: "
            "слои, Excel и Word")
        self.action.triggered.connect(self.run)
        altan_toolbar.add_action(self.iface, self.action)
        altan_toolbar.add_to_menu(self.iface, self.action)

    def unload(self):
        if self._running is not None:
            self._running[0].cancel()
            self._running = None
        if self.dialog is not None:
            self.dialog.cleanup()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        if self.action:
            altan_toolbar.remove_from_menu(self.iface, self.action)
            altan_toolbar.remove_action(self.iface, self.action)
            self.action.deleteLater()
            self.action = None
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    # ------------------------------------------------------------ окно

    def message(self, text, level=Qgis.MessageLevel.Info, duration=6):
        self.iface.messageBar().pushMessage(TITLE, text, level, duration)

    def run(self):
        if self.dialog is None:
            from .dialog import SzzDialog

            self.dialog = SzzDialog(self.iface, self.start, self.load_nspd,
                                    enterprise_callback=self.load_enterprise)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def start(self, params, dialog=None, algorithm_id=ALGORITHM, description=TITLE, done=None):
        if self._running is not None:
            self.message("Расчёт ещё идёт — ход виден внизу окна QGIS.")
            return
        algorithm = QgsApplication.processingRegistry().createAlgorithmById(algorithm_id)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = _Feedback()
        # MAP_PDF — не параметр алгоритма: карту строит модуль после расчёта (нужны слои проекта)
        run_params = {k: v for k, v in params.items() if k != "MAP_PDF"}
        task = QgsProcessingAlgRunnerTask(algorithm, run_params, context, feedback)
        task.setDescription(description)
        self._running = (task, context, feedback, params)
        task.executed.connect(done or self.finished)
        if dialog is not None:
            dialog.run_button.setEnabled(False)
            dialog.nspd_button.setEnabled(False)
            dialog.cad_button.setEnabled(False)
        QgsApplication.taskManager().addTask(task)

    def load_nspd(self, params, dialog=None):
        self.start(params, dialog, NSPD_ALGORITHM, "Загрузка участков с НСПД", self.nspd_finished)

    def load_enterprise(self, params, dialog=None):
        self.start(params, dialog, ENTERPRISE_ALGORITHM, "Участок предприятия с НСПД",
                   self.enterprise_finished)

    def _take_running(self):
        running, self._running = self._running, None
        if self.dialog is not None:
            self.dialog.run_button.setEnabled(self.dialog.error is None)
            self.dialog.update_nspd_button()
        return running

    def _make_map(self, params, layers, zone, from_enterprise, distance, path, szz_size=None,
                  width=0, points=None, crs_name=""):
        """Макеты и PDF: «Карта СЗЗ» (участки), «Карта градостроительного зонирования» (если
        выбраны зоны ПЗЗ), «Схема границы СЗЗ» (точки и каталог координат). У каждого макета —
        одноимённая тема карты. True — главная карта записана; прочие PDF — в self.extra_pdfs."""
        project = QgsProject.instance()

        def layer_of(value):
            if isinstance(value, QgsProcessingFeatureSourceDefinition):
                value = value.source.staticValue()
            return project.mapLayer(value) if isinstance(value, str) else None

        enterprise = layer_of(params.get("ENTERPRISE"))
        szz = szz_size or layer_of(params.get("SZZ"))  # сглаженная — вместо исходной
        original = layer_of(params.get("SZZ")) if szz_size is not None else None
        terr = layer_of(params.get("TERR_ZONES"))
        crs = project.crs() if project.crs().isValid() else zone.crs()

        def fit(layer, grow):
            box = QgsCoordinateTransform(layer.crs(), crs, project).transformBoundingBox(layer.extent())
            box.grow(grow * max(box.width(), box.height()))
            return box

        extent = fit(zone, 0.05)
        subtitle = ("СЗЗ совпадает с границей предприятия; " if from_enterprise else
                    "СЗЗ — {:g} м от границы предприятия; ".format(width) if width else "") + \
            "участки в границах СЗЗ и за её границей на расстоянии до {:g} м".format(distance)
        border = [(enterprise, "Граница предприятия"), (szz, "Санитарно-защитная зона")]
        aside = [l for l in (terr, points, original) if l is not None]  # у них свои карты
        self.extra_pdfs = []
        layout = mapmaker.build_layout(
            project, "Схема расположения земельных участков в санитарно-защитной зоне", subtitle,
            extent, border + [(layer, None) for layer in layers] + [(zone, None)], crs,
            params.get("OBJECT", ""), hidden=aside)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            return False
        if not mapmaker.export_pdf(layout, path):
            return False
        folder, stem = os.path.split(path)
        # карта градостроительного зонирования — тот же охват и рамка, зоны ПЗЗ вместо участков
        if terr is not None:
            layout = mapmaker.build_layout(
                project, "Карта градостроительного зонирования (территориальные зоны по ЕГРН)",
                subtitle, extent, border + [(zone, None), (terr, None)], crs, params.get("OBJECT", ""),
                mapmaker.ZONING_LAYOUT, rasters_only=True)
            zoning = os.path.join(folder, stem.replace(mapmaker.LAYOUT_NAME, mapmaker.ZONING_LAYOUT))
            if mapmaker.export_pdf(layout, zoning):
                self.extra_pdfs.append(zoning)
        # схема границы СЗЗ: поворотные точки и рядом каталог координат (пожелание 2026-10-27)
        if points is not None and points.featureCount():
            rows = []  # каждый контур замыкается своей первой точкой, как в каталоге Word
            for feature in sorted(points.getFeatures(), key=lambda f: f["num"]):
                row = (feature["num"], feature["x"], feature["y"])
                if rows and feature["contour"] != contour:
                    rows.append(first)
                if not rows or feature["contour"] != contour:
                    first, contour = row, feature["contour"]
                rows.append(row)
            rows.append(first)
            outline = szz or enterprise
            layout = mapmaker.build_layout(
                project, "Схема границы санитарно-защитной зоны с каталогом координат",
                "координаты характерных точек — {}".format(crs_name or points.crs().description()),
                fit(outline, 0.15), [(enterprise, "Граница предприятия"), (szz, "Граница СЗЗ"),
                                     (points, "Характерные точки границы СЗЗ")],
                crs, params.get("OBJECT", ""), mapmaker.BORDER_LAYOUT, rasters_only=True,
                table=("Каталог координат ({})".format(crs_name or points.crs().description()), rows))
            scheme = os.path.join(folder, stem.replace(mapmaker.LAYOUT_NAME, mapmaker.BORDER_LAYOUT))
            if mapmaker.export_pdf(layout, scheme):
                self.extra_pdfs.append(scheme)
        return True

    def nspd_finished(self, ok, results):
        running = self._take_running()
        if running is None:
            return
        task, context, feedback, params = running
        if not ok:
            if feedback.isCanceled():
                self.message("Загрузка с НСПД отменена.", Qgis.MessageLevel.Warning)
            else:
                text = feedback.errors[-1] if feedback.errors else "Не удалось загрузить участки с НСПД."
                self.message(text, Qgis.MessageLevel.Critical, 0)
            return
        path = results.get("OUTPUT")
        name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsVectorLayer(path, name, "ogr")
        if not layer.isValid():
            self.message("Участки загружены, но файл «{}» не открылся.".format(path),
                         Qgis.MessageLevel.Critical, 0)
            return
        # стиль пользователя (2026-10-24): цвета по категориям земель, подписи «:номер»
        message, styled = layer.loadNamedStyle(NSPD_STYLE)
        if not styled:
            QgsMessageLog.logMessage("Стиль участков НСПД не применён: {}".format(message), TITLE)
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(0, layer)
        terr_path = results.get("TERR_OUT")
        terr = QgsVectorLayer(terr_path, os.path.splitext(os.path.basename(terr_path))[0], "ogr") \
            if terr_path else None
        if terr is not None and terr.isValid() and terr.featureCount():
            mapmaker.style_terr_zones(terr)
            project.addMapLayer(terr, False)
            node = project.layerTreeRoot().insertLayer(1, terr)
            node.setItemVisibilityChecked(False)  # под участками не видно — включается для карты ПЗЗ
            if self.dialog is not None:
                self.dialog.terr_zones.setLayer(terr)
        count = results.get("COUNT", layer.featureCount())
        if self.dialog is not None:
            self.dialog.parcels.setLayer(layer)
        if not count:
            self.message("НСПД не нашёл в зоне поиска участков с границами. Увеличьте расстояние "
                         "поиска или проверьте границу предприятия.", Qgis.MessageLevel.Warning, 0)
            return
        zones_text = ""
        if terr_path:
            zones_text = (" Территориальных зон (ПЗЗ): {}.".format(results.get("TERR_COUNT", 0))
                          if results.get("TERR_COUNT") else
                          " Территориальных зон в ЕГРН здесь нет (ПЗЗ внесены не везде).")
        self.message("Загружено участков с НСПД: {} — слой «{}», файл в папке «{}». Он уже выбран "
                     "слоем участков.{} Сведения кадастровой карты справочные и не заменяют выписку "
                     "из ЕГРН.".format(count, name, os.path.basename(os.path.dirname(path)), zones_text),
                     Qgis.MessageLevel.Success, 15)

    def enterprise_finished(self, ok, results):
        running = self._take_running()
        if running is None:
            return
        task, context, feedback, params = running
        if not ok:
            if not feedback.isCanceled():
                self.message(feedback.errors[-1] if feedback.errors else
                             "Не удалось загрузить участок с НСПД.", Qgis.MessageLevel.Critical, 0)
            return
        path = results.get("OUTPUT")
        layer = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
        if not layer.isValid():
            self.message("Участок загружен, но файл «{}» не открылся.".format(path),
                         Qgis.MessageLevel.Critical, 0)
            return
        mapmaker.style_enterprise(layer)
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(0, layer)
        if self.dialog is not None:
            self.dialog.enterprise.setLayer(layer)
            self.dialog.enterprise_selected.setChecked(False)
        canvas = self.iface.mapCanvas()
        extent = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(),
                                        project).transformBoundingBox(layer.extent())
        extent.grow(0.5 * max(extent.width(), extent.height()))
        canvas.setExtent(extent)
        canvas.refresh()
        text = "Граница предприятия загружена с НСПД: слой «{}», выбран в окне.".format(layer.name())
        if results.get("MISSING"):
            text += " Не найдены (или без границ): {}.".format(results["MISSING"])
        self.message(text, Qgis.MessageLevel.Success if not results.get("MISSING")
                     else Qgis.MessageLevel.Warning, 10)

    def finished(self, ok, results):
        running = self._take_running()
        if running is None:
            return
        task, context, feedback, params = running
        if not ok:
            if feedback.isCanceled():
                self.message("Расчёт отменён.", Qgis.MessageLevel.Warning)
            else:
                text = feedback.errors[-1] if feedback.errors else "Не удалось составить реестр."
                self.message(text, Qgis.MessageLevel.Critical, 0)
            return
        from .processing.registry_algorithm import INSIDE_ALIASES, apply_aliases, near_aliases

        from_enterprise = bool(results.get("FROM_ENTERPRISE"))
        distance = params.get("DISTANCE", 0)
        names = {
            "INSIDE": "В границах СЗЗ",
            "NEAR": "За границей {} до {:g} м".format(
                "предприятия" if from_enterprise else "СЗЗ", distance),
        }
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(GROUP) or root.insertGroup(0, GROUP)
        # прошлые расчёты в группе выключаются (не удаляются — могут быть сохранены в файлы),
        # иначе на карте видны вперемешку старые и новые участки
        for node in group.findLayers():
            node.setItemVisibilityChecked(False)
        layers = []
        for key in ("INSIDE", "NEAR"):
            layer = context.takeResultLayer(results[key])
            if layer is None:
                continue
            layer.setName(names[key])
            layer.setCustomProperty("szz_parcels/result", True)  # окно не подставит его границей
            apply_aliases(layer, INSIDE_ALIASES if key == "INSIDE" else near_aliases(from_enterprise))
            symbol = layer.renderer().symbol() if layer.renderer() else None
            if symbol is not None:  # те же цвета, что у подсветки в окне
                symbol.setColor(QColor(214, 39, 40, 110) if key == "INSIDE"
                                else QColor(255, 140, 0, 110))
            mapmaker.label_cadastral_end(layer)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            layers.append(layer)
        points = context.takeResultLayer(results["POINTS"]) if results.get("POINTS") else None
        if points is not None:
            name = results.get("COORD_CRS_NAME", "МСК")
            points.setName("Точки границы СЗЗ ({})".format(name))
            # СК слоя — МСК под своим названием (как в «СК проекта»), а не «unknown»:
            # регистрируется здесь, в основном потоке, а не в фоновой задаче расчёта
            item = next((i for i in msk.items() if msk.short_name(i) == name), None)
            if item is not None:
                points.setCrs(msk.crs_for(item))
            points.setCustomProperty("szz_parcels/result", True)
            for field, alias in (("num", "№ точки"), ("x", "X, м"), ("y", "Y, м"), ("contour", "Контур")):
                points.setFieldAlias(points.fields().indexOf(field), alias)
            mapmaker.style_points(points)
            project.addMapLayer(points, False)
            group.insertLayer(0, points)
        normed = context.takeResultLayer(results["NORMED_OUT"]) if results.get("NORMED_OUT") else None
        if normed is not None and normed.featureCount():  # пустой слой — шум: число есть в сообщении
            normed.setName("Нормируемые территории в СЗЗ")
            normed.setCustomProperty("szz_parcels/result", True)
            apply_aliases(normed, INSIDE_ALIASES)
            mapmaker.style_normed(normed)
            project.addMapLayer(normed, False)
            group.insertLayer(0, normed)
            layers.append(normed)
        szz_size = context.takeResultLayer(results["SZZ_OUT"]) if results.get("SZZ_OUT") else None
        if szz_size is not None:
            szz_size.setName("СЗЗ {}{:g} м от границы предприятия".format(
                "сглаженная, " if results.get("SMOOTHED") else "", results.get("SZZ_SIZE", 0)))
            szz_size.setCustomProperty("szz_parcels/result", True)
            mapmaker.style_szz(szz_size)
            project.addMapLayer(szz_size, False)
            group.insertLayer(0, szz_size)
            if results.get("SMOOTHED"):  # исходная квадратная СЗЗ на карте не нужна — только новая
                source = params.get("SZZ")
                if isinstance(source, QgsProcessingFeatureSourceDefinition):
                    source = source.source.staticValue()
                original = root.findLayer(source) if isinstance(source, str) else None
                if original is not None:
                    original.setItemVisibilityChecked(False)
        zone = context.takeResultLayer(results["SEARCH"]) if results.get("SEARCH") else None
        if zone is not None:
            zone.setName("Зона поиска до {:g} м".format(distance))
            zone.setCustomProperty("szz_parcels/result", True)
            mapmaker.style_search_zone(zone)
            project.addMapLayer(zone, False)
            group.insertLayer(0, zone)  # сверху: пунктир не прячется под заливкой участков

        text = "В границах СЗЗ: {}, за границей до {:g} м: {}.".format(
            results.get("INSIDE_COUNT", 0), distance, results.get("NEAR_COUNT", 0))
        if params.get("NORMED"):
            text += " Нормируемых территорий в СЗЗ: {}.".format(results.get("NORMED_COUNT", 0))
        files = [results[k] for k in ("XLSX", "DOCX") if results.get(k)]
        if files:
            text += " Файлы: {} — в папке «{}».".format(
                ", ".join("«{}»".format(os.path.basename(f)) for f in files),
                os.path.basename(os.path.dirname(files[0])))
        if results.get("ZOUIT_XML"):
            text += " XML для ЕГРН: «{}».".format(os.path.basename(results["ZOUIT_XML"]))
        map_pdf = params.get("MAP_PDF")
        if map_pdf and zone is not None:
            if self._make_map(params, layers, zone, from_enterprise, distance, map_pdf, szz_size,
                              results.get("SZZ_SIZE", 0), points, results.get("COORD_CRS_NAME", "")):
                files.append(map_pdf)
                text += " Карты: {}.".format(", ".join("«{}»".format(os.path.basename(f))
                                                       for f in [map_pdf] + self.extra_pdfs))
                files.extend(self.extra_pdfs)
            else:
                feedback.warnings.append(
                    "Карту «{}» записать не удалось: файл, похоже, открыт или папка недоступна. "
                    "Макет «Карта СЗЗ» в проекте создан — выведите его через «Проект → Макеты»."
                    .format(os.path.basename(map_pdf)))
        widget = self.iface.messageBar().createMessage(TITLE, text)
        for path in files:
            name = os.path.basename(path)
            button = QPushButton("Открыть {}".format(
                {".xlsx": "Excel", ".docx": "Word"}.get(os.path.splitext(path)[1]) or
                ("карту ПЗЗ" if name.startswith(mapmaker.ZONING_LAYOUT) else
                 "схему границы" if name.startswith(mapmaker.BORDER_LAYOUT) else "карту СЗЗ")))
            button.clicked.connect(lambda _=False, p=path: QDesktopServices.openUrl(
                QUrl.fromLocalFile(p)))
            widget.layout().addWidget(button)
        self.iface.messageBar().pushWidget(widget, Qgis.MessageLevel.Success, 0)
        if feedback.warnings:
            self.message(" ".join(feedback.warnings), Qgis.MessageLevel.Warning, 0)
        return layers
