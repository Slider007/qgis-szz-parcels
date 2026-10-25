"""Алгоритм «Реестр участков в СЗЗ»: участки в границах СЗЗ и ближайшие за ними."""

import datetime
import os

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDistance,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from .. import core, export, msk

# msk.py и msk.json — копии из модуля project_utm_crs, правятся там; представляемся своим именем
msk.USER_AGENT = "QGIS plugin szz_parcels (https://github.com/Slider007/qgis-szz-parcels)"
# Тесты подменяют запрос к сервису адресов (Nominatim): функция (lon, lat) → (ответ, ошибка)
GEOCODE = None

INSIDE_ALIASES = {"num": "№ п/п", "cad_num": "Кадастровый номер", "category": "Категория земель",
                  "vri_doc": "ВРИ по документу", "szz_pct": "Доля участка в СЗЗ, %",
                  "dir": "Направление от предприятия"}
DIRECTION_FROM = ("от центра предприятия", "от ближайшей точки границы предприятия")


def near_aliases(from_enterprise):
    names = dict(INSIDE_ALIASES)
    del names["szz_pct"]
    names["dist_m"] = ("Расстояние до границы предприятия, м" if from_enterprise
                       else "Расстояние до СЗЗ, м")
    return names


def apply_aliases(layer, names):
    for field, alias in names.items():
        index = layer.fields().indexOf(field)
        if index >= 0:
            layer.setFieldAlias(index, alias)


class _Aliases(QgsProcessingLayerPostProcessorInterface):
    """Ставит заголовки столбцов, когда Processing добавляет слой в проект."""

    instances = []

    def __init__(self, names):
        super().__init__()
        self.names = names

    def postProcessLayer(self, layer, context, feedback):
        apply_aliases(layer, self.names)

    @classmethod
    def create(cls, names):
        # Processing не владеет объектом: держим ссылку, иначе Python его удалит
        cls.instances = cls.instances[-9:] + [cls(names)]
        return cls.instances[-1]


class SzzRegistryAlgorithm(QgsProcessingAlgorithm):
    PARCELS = "PARCELS"
    CAD_FIELD = "CAD_FIELD"
    CATEGORY_FIELD = "CATEGORY_FIELD"
    VRI_DOC_FIELD = "VRI_DOC_FIELD"
    ENTERPRISE = "ENTERPRISE"
    SZZ = "SZZ"
    DISTANCE = "DISTANCE"
    SHARE = "SHARE"
    DIRECTION = "DIRECTION"
    DIRECTION_FROM = "DIRECTION_FROM"
    SEARCH = "SEARCH"
    COORDS = "COORDS"
    COORD_CRS = "COORD_CRS"
    POINTS = "POINTS"
    OBJECT = "OBJECT"
    ENT_SHARE = "ENT_SHARE"
    MIN_OVERLAP = "MIN_OVERLAP"
    DEPTH_PARAMS = {"settlement": "DEPTH_SETTLEMENT", "garden": "DEPTH_GARDEN",
                    "industry": "DEPTH_INDUSTRY", "agri": "DEPTH_AGRI", "forest": "DEPTH_FOREST",
                    "other": "DEPTH_OTHER"}
    INSIDE = "INSIDE"
    NEAR = "NEAR"
    XLSX = "XLSX"
    DOCX = "DOCX"

    def tr(self, text):
        return QCoreApplication.translate("SzzParcels", text)

    def createInstance(self):
        return SzzRegistryAlgorithm()

    def name(self):
        return "registry"

    def displayName(self):
        return self.tr("Реестр участков в СЗЗ")

    def group(self):
        return self.tr("Санитарно-защитные зоны")

    def groupId(self):
        return "szz"

    def shortHelpString(self):
        return self.tr(
            "Две таблицы земельных участков: в границах СЗЗ и ближайшие за её границей в "
            "пределах заданного расстояния, с кадастровым номером, категорией земель, ВРИ по "
            "документу, долей участка в СЗЗ и расстоянием до СЗЗ.\n\n"
            "Если слой СЗЗ не задан, СЗЗ совпадает с границей предприятия, и расстояния "
            "отсчитываются от неё. Граница предприятия — полигоны или замкнутые линии "
            "(например, из чертежа); все объекты слоя объединяются, как и все зоны слоя СЗЗ "
            "(чтобы взять часть — «только выделенные»).\n\n"
            "Участки самого предприятия (больше половины площади внутри его границы) в "
            "таблицы не входят. Участок, частично заходящий в СЗЗ, относится к первой таблице. "
            "Контуры с одним кадастровым номером (многоконтурный участок, повтор в выгрузке) "
            "дают одну строку.\n\n"
            "Расстояние — кратчайшее от участка до границы СЗЗ, на плоскости СК слоя участков "
            "(если он в градусах — в зоне UTM по месту).\n\n"
            "Таблицы можно сохранить в Excel (два листа) и в Word (A4).")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PARCELS, self.tr("Земельные участки"),
            [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.CAD_FIELD, self.tr("Поле кадастрового номера (пусто — найти по названию)"),
            parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.CATEGORY_FIELD, self.tr("Поле категории земель (пусто — найти по названию)"),
            parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.VRI_DOC_FIELD, self.tr("Поле ВРИ по документу (пусто — найти по названию)"),
            parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ENTERPRISE, self.tr("Граница предприятия (полигоны или замкнутые линии)"),
            [Qgis.ProcessingSourceType.VectorPolygon, Qgis.ProcessingSourceType.VectorLine]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SZZ, self.tr("СЗЗ (пусто — СЗЗ по границе предприятия)"),
            [Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterDistance(
            self.DISTANCE, self.tr("Искать участки за границей СЗЗ на расстоянии до, м"),
            100.0, self.PARCELS, False, 0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SHARE, self.tr("Указывать долю участка в СЗЗ, %"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DIRECTION, self.tr("Указывать направление от предприятия (румб)"), True))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIRECTION_FROM, self.tr("Направление считать"),
            options=[self.tr(o) for o in DIRECTION_FROM], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.OBJECT, self.tr("Название предприятия для заголовка в Word"), optional=True))
        for name, text, default, maximum in (
                (self.ENT_SHARE, "Участок предприятия: внутри его границы не меньше, %",
                 core.ENTERPRISE_SHARE, 100.0),
                (self.MIN_OVERLAP, "Не считать попаданием в СЗЗ заход меньше, м²",
                 core.MIN_OVERLAP_M2, 1e9)) + tuple(
                (self.DEPTH_PARAMS[key], "Допуск расхождения границ, м: " + core.DEPTH_NAMES[key],
                 core.DEPTHS[key], 1000.0) for key in core.DEPTHS):
            param = QgsProcessingParameterNumber(
                name, self.tr(text), Qgis.ProcessingNumberParameterType.Double, default,
                False, 0.0, maximum)
            param.setFlags(param.flags() | Qgis.ProcessingParameterFlag.Advanced)
            self.addParameter(param)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.INSIDE, self.tr("Участки в границах СЗЗ"),
            Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.NEAR, self.tr("Участки за границей СЗЗ"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SEARCH, self.tr("Зона поиска"), Qgis.ProcessingSourceType.VectorPolygon,
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COORDS, self.tr("Каталог координат границы СЗЗ"), True))
        self.addParameter(QgsProcessingParameterCrs(
            self.COORD_CRS, self.tr("Система координат каталога (пусто — МСК субъекта по месту)"),
            optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, self.tr("Точки границы СЗЗ"), Qgis.ProcessingSourceType.VectorPoint,
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.XLSX, self.tr("Таблицы в Excel"), self.tr("Excel (*.xlsx)"),
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.DOCX, self.tr("Таблицы в Word"), self.tr("Word (*.docx)"),
            optional=True, createByDefault=False))

    # ---------------------------------------------------------------- расчёт

    def _field(self, parameters, name, context, source, candidates):
        field = self.parameterAsString(parameters, name, context)
        if field and source.fields().indexOf(field) < 0:
            raise QgsProcessingException(self.tr(
                "В слое участков нет поля «{}» — выберите поле из списка в параметрах.").format(field))
        return field or core.find_field(source.fields().names(), candidates)

    def _geometries(self, source, crs, context, feedback):
        to_crs = None
        if source.sourceCrs() != crs:
            to_crs = QgsCoordinateTransform(source.sourceCrs(), crs, context.transformContext())
        result = []
        for feature in source.getFeatures():
            if feedback.isCanceled():
                break
            geometry = feature.geometry()
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                continue
            geometry = QgsGeometry(geometry)
            if to_crs is not None:
                geometry.transform(to_crs)
            result.append((feature, geometry))
        return result

    def _area(self, pairs, what):
        try:
            geometry = core.area_geometry([g for _, g in pairs])
        except ValueError:
            raise QgsProcessingException(self.tr(
                "{}: среди линий есть незамкнутая. Замкните контур или нарисуйте полигон.")
                .format(what))
        if geometry is None:
            raise QgsProcessingException(self.tr(
                "{}: нет ни одного контура — проверьте слой; если взяты только выделенные "
                "объекты, выделите нужные.").format(what))
        return geometry

    def _save(self, feedback, path, program, write, *args):
        """Записать файл; не вышло (открыт в Excel/Word, нет доступа) — предупреждение по-русски,
        расчёт не обрывается: слои всё равно попадают в проект."""
        try:
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            write(path, *args)
            return True
        except OSError:
            feedback.pushWarning(self.tr(
                "Не удалось сохранить «{}»: файл, похоже, открыт в {} или папка недоступна — "
                "закройте его и нажмите «Сформировать» ещё раз. Слои в проекте уже созданы.")
                .format(os.path.basename(path), program))
            return False

    def _coord_crs(self, parameters, context, zone, work_crs, sources, feedback):
        """(СК каталога, её название) или (None, None) с предупреждением.

        Задана вручную — она; слой границы/СЗЗ/участков уже в МСК из набора — его МСК;
        иначе МСК субъекта по месту (один запрос к сервису адресов по центру СЗЗ)."""
        fixed = self.parameterAsCrs(parameters, self.COORD_CRS, context)
        if fixed.isValid():
            if fixed.isGeographic():
                feedback.pushWarning(self.tr("Каталог координат СЗЗ не составлен: система «{}» в "
                                             "градусах — выберите МСК в «Дополнительно».")
                                     .format(fixed.description()))
                return None, None
            item = msk.item_for_crs(fixed)
            return fixed, msk.short_name(item) if item else fixed.description()
        ambiguous = []
        for source in sources:
            if source is None:
                continue
            crs = source.sourceCrs()
            same = _msk_items(crs)
            named = [i for i in same if i["name"] in crs.description()]
            if len(same) == 1 or len(named) == 1:
                item = (named or same)[0]
                return msk.crs_for(item, register=False), msk.short_name(item)
            if same and not ambiguous:
                ambiguous = same  # у МСК-01 и МСК-23 зона 1 параметры одинаковые
        centre = QgsGeometry(zone.centroid())
        centre.transform(QgsCoordinateTransform(work_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                                context.transformContext()))
        point = centre.asPoint()
        geocode = GEOCODE or msk.reverse_geocode
        answer, error = geocode(point.x(), point.y())
        code = msk.region_from_reverse(answer)[0] if not error else None
        zones = msk.zones_for(code, point.x()) if code is not None else []
        if ambiguous:
            # слой уже в одной из этих МСК: субъект по месту только выбирает название
            chosen = [i for i in ambiguous if i["code"] == code]
            item = chosen[0] if chosen else ambiguous[0]
            name = msk.short_name(item) if chosen else " / ".join(msk.short_name(i) for i in ambiguous)
            return msk.crs_for(item, register=False), name
        if not zones:
            reason = error or self.tr("субъект РФ по месту не найден в наборе МСК")
            feedback.pushWarning(self.tr(
                "МСК для координат СЗЗ не определена: {} Выберите систему координат каталога в "
                "окне («Дополнительно») и нажмите «Сформировать» ещё раз.").format(
                    reason.rstrip(".") + "."))
            return None, None
        return msk.crs_for(zones[0], register=False), msk.short_name(zones[0])

    def processAlgorithm(self, parameters, context, feedback):
        # неверные контуры (самопересечения в выгрузках ЕГРН) исправляются в core через
        # makeValid, а не обрывают расчёт
        context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.NoCheck)
        parcels = self.parameterAsSource(parameters, self.PARCELS, context)
        if parcels is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.PARCELS))
        enterprise_src = self.parameterAsSource(parameters, self.ENTERPRISE, context)
        if enterprise_src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.ENTERPRISE))
        szz_src = self.parameterAsSource(parameters, self.SZZ, context)
        from_enterprise = szz_src is None
        distance = self.parameterAsDouble(parameters, self.DISTANCE, context)
        share = self.parameterAsBoolean(parameters, self.SHARE, context)
        with_direction = self.parameterAsBoolean(parameters, self.DIRECTION, context)
        from_boundary = self.parameterAsEnum(parameters, self.DIRECTION_FROM, context) == 1
        title_object = self.parameterAsString(parameters, self.OBJECT, context).strip()

        cad_field = self._field(parameters, self.CAD_FIELD, context, parcels, core.CAD_FIELDS)
        if not cad_field:
            raise QgsProcessingException(self.tr(
                "Не найдено поле кадастрового номера: выберите его в параметрах."))
        category_field = self._field(parameters, self.CATEGORY_FIELD, context, parcels,
                                     core.CATEGORY_FIELDS)
        vri_field = self._field(parameters, self.VRI_DOC_FIELD, context, parcels,
                                core.VRI_DOC_FIELDS)
        feedback.pushInfo(self.tr("Поля: кадастровый номер — «{}», категория — «{}», "
                                  "ВРИ по документу — «{}»").format(
            cad_field, category_field or "—", vri_field or "—"))
        if not category_field:
            feedback.pushWarning(self.tr("В слое участков нет поля категории земель: "
                                         "в столбце «Категория не установлена»."))
        if not vri_field:
            feedback.pushWarning(self.tr("В слое участков нет поля ВРИ по документу: "
                                         "в столбце прочерки."))

        # 1. Граница предприятия и СЗЗ — в СК расчёта (метры)
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        ent_pairs = self._geometries(enterprise_src, wgs, context, feedback)
        work_crs = core.working_crs(
            parcels.sourceCrs(), QgsGeometry.unaryUnion([g for _, g in ent_pairs]))
        if parcels.sourceCrs().isGeographic():
            feedback.pushInfo(self.tr("Слой участков в градусах: расчёт в {}")
                              .format(work_crs.authid()))
        enterprise = self._area(self._geometries(enterprise_src, work_crs, context, feedback),
                                self.tr("Граница предприятия"))
        zone = None
        zone_names = []
        if szz_src is not None:
            zone_pairs = self._geometries(szz_src, work_crs, context, feedback)
            zone = self._area(zone_pairs, self.tr("СЗЗ"))
            name_field = core.find_field(szz_src.fields().names(), core.ZONE_NAME_FIELDS)
            if name_field:
                for feature, _ in zone_pairs:
                    name = core.text(feature[name_field])
                    if name and name not in zone_names:
                        zone_names.append(name)
            if not zone.intersects(enterprise):
                feedback.pushWarning(self.tr(
                    "СЗЗ не касается границы предприятия — проверьте, та ли зона выбрана."))
        if feedback.isCanceled():
            return {}
        classifier = core.Classifier(
            enterprise, zone, distance,
            self.parameterAsDouble(parameters, self.ENT_SHARE, context),
            self.parameterAsDouble(parameters, self.MIN_OVERLAP, context),
            depths={key: self.parameterAsDouble(parameters, name, context)
                    for key, name in self.DEPTH_PARAMS.items()})

        # 2. Участки в зоне поиска
        request = core.parcel_request(classifier.search_area(), work_crs, parcels.sourceCrs(),
                                      context.transformContext())
        to_work = None
        if parcels.sourceCrs() != work_crs:
            to_work = QgsCoordinateTransform(parcels.sourceCrs(), work_crs,
                                             context.transformContext())
        # ход — от участков в зоне поиска, а не от всего слоя (выгрузка бывает на весь район)
        candidates = list(parcels.getFeatures(request))
        rows, empty = core.collect(candidates, cad_field, to_work, classifier,
                                   feedback, len(candidates), category_field, vri_field)
        if rows is None:
            return {}
        if empty:
            feedback.pushWarning(self.tr("Участков без геометрии пропущено: {}").format(empty))
        inside, near, own = core.split(rows, classifier)
        if own:
            feedback.pushInfo(self.tr("Участки предприятия (в таблицы не вошли): {}").format(
                ", ".join(r["cad"] or "без номера" for r, _ in own)))
        feedback.pushInfo(self.tr("В границах СЗЗ: {}, за границей в пределах {:g} м: {}")
                          .format(len(inside), distance, len(near)))

        # зона поиска — слоем: для карты и легенды
        search_fields = QgsFields()
        search_fields.append(QgsField("dist_m", QMetaType.Type.Double))
        search_sink, search_id = self.parameterAsSink(
            parameters, self.SEARCH, context, search_fields, Qgis.WkbType.MultiPolygon,
            parcels.sourceCrs())
        if search_sink is not None:
            area = classifier.search_area()
            if work_crs != parcels.sourceCrs():
                area.transform(QgsCoordinateTransform(work_crs, parcels.sourceCrs(),
                                                      context.transformContext()))
            area.convertToMultiType()
            zone_feature = QgsFeature(search_fields)
            zone_feature.setGeometry(area)
            zone_feature.setAttributes([distance])
            search_sink.addFeature(zone_feature, QgsFeatureSink.Flag.FastInsert)
            if hasattr(search_sink, "finalize"):
                search_sink.finalize()

        # каталог координат границы СЗЗ (по выбору пользователя 2026-10-25: МСК по месту,
        # с северо-западной точки по часовой; таблица 3, лист Excel, слой точек, номера на карте)
        coord_rows, coord_name, coord_area, results_points = [], None, None, None
        if self.parameterAsBoolean(parameters, self.COORDS, context):
            coord_crs, coord_name = self._coord_crs(
                parameters, context, classifier.zone, work_crs,
                (szz_src, enterprise_src, parcels), feedback)
            if coord_crs is not None:
                outline = QgsGeometry(classifier.zone)
                outline.transform(QgsCoordinateTransform(work_crs, coord_crs,
                                                         context.transformContext()))
                coord_rows = core.szz_points(outline)
                coord_area = outline.area()
                point_fields = QgsFields()
                point_fields.append(QgsField("num", QMetaType.Type.Int))
                for name in ("x", "y"):
                    field = QgsField(name, QMetaType.Type.Double)
                    field.setLength(20)
                    field.setPrecision(2)
                    point_fields.append(field)
                point_fields.append(QgsField("contour", QMetaType.Type.Int))
                points_sink, points_id = self.parameterAsSink(
                    parameters, self.POINTS, context, point_fields, Qgis.WkbType.Point, coord_crs)
                if points_sink is not None:
                    written = set()  # замыкающая строка каталога — та же точка, в слой не идёт
                    for number, x, y, contour in coord_rows:
                        if number in written:
                            continue
                        written.add(number)
                        feature = QgsFeature(point_fields)
                        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(y, x)))
                        feature.setAttributes([number, x, y, contour])
                        points_sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
                    if hasattr(points_sink, "finalize"):
                        points_sink.finalize()
                    results_points = points_id

        # 3. Слои
        def attributes(row):
            # пустые ячейки заполняются по просьбе пользователя (2026-09-29)
            feature = row["feature"]
            category = feature[category_field] if category_field else None
            vri = core.text(feature[vri_field]) if vri_field else None
            return [row["cad"] or core.NO_VALUE, core.category_text(category),
                    vri or core.NO_VALUE]

        def rhumb(row):
            parcel = row["work"][0] if len(row["work"]) == 1 else QgsGeometry.unaryUnion(row["work"])
            return core.direction(enterprise, parcel, from_boundary) or core.NO_VALUE

        def fields(last):
            result = QgsFields()
            result.append(QgsField("num", QMetaType.Type.Int))
            for name in ("cad_num", "category", "vri_doc"):
                result.append(QgsField(name, QMetaType.Type.QString))
            if last:
                field = QgsField(last, QMetaType.Type.Double)
                field.setLength(20)
                field.setPrecision(1)
                result.append(field)
            if with_direction:
                result.append(QgsField("dir", QMetaType.Type.QString))
            return result

        inside_fields = fields("szz_pct" if share else None)
        near_fields = fields("dist_m")
        results = {}
        tables = []
        for key, items, layer_fields, value_name in (
                (self.INSIDE, inside, inside_fields, "szz_pct" if share else None),
                (self.NEAR, near, near_fields, "dist_m")):
            sink, dest_id = self.parameterAsSink(
                parameters, key, context, layer_fields, Qgis.WkbType.MultiPolygon,
                parcels.sourceCrs())
            if sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, key))
            table_rows = []
            for n, (row, value) in enumerate(items, 1):
                values = [n] + attributes(row)
                if value_name:
                    values.append(round(value, 1))
                if with_direction:
                    values.append(rhumb(row))
                table_rows.append(values)
                feature = QgsFeature(layer_fields)
                geometry = row["geoms"][0] if len(row["geoms"]) == 1 else \
                    QgsGeometry.unaryUnion(row["geoms"])
                geometry = QgsGeometry(geometry)
                geometry.convertToMultiType()
                feature.setGeometry(geometry)
                feature.setAttributes(values)
                sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
            if hasattr(sink, "finalize"):
                sink.finalize()
            names = INSIDE_ALIASES if key == self.INSIDE else near_aliases(from_enterprise)
            if context.willLoadLayerOnCompletion(dest_id):
                context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(
                    _Aliases.create(names))
            results[key] = dest_id
            tables.append(([names[f.name()] for f in layer_fields], table_rows))

        # 4. Файлы
        zone_word = self.tr("границы предприятия") if from_enterprise else self.tr("СЗЗ")
        titles = [self.tr("Таблица 1. Земельные участки в границах санитарно-защитной зоны"),
                  self.tr("Таблица 2. Земельные участки за границей {} на расстоянии до {:g} м")
                  .format(self.tr("предприятия") if from_enterprise
                          else self.tr("санитарно-защитной зоны"), distance)]
        export_tables = []
        for i, (headers, table_rows) in enumerate(tables):
            has_value = (i == 1) or share
            value_col = 4 if has_value else None
            # направление — 18: «предприятия» в шапке не рвётся по слогам; место — у ВРИ
            vri_w = 28 if with_direction else 32
            # «№ п/п» — 9: при 7 шапка переносилась на две строки
            widths = [9, 21, 22, vri_w] + ([17] if has_value else []) + ([18] if with_direction else [])
            excel = [7, 22, 30, 50] + ([16] if has_value else []) + ([14] if with_direction else [])
            export_tables.append({
                "title": titles[i],
                "sheet": ["В границах СЗЗ", "За границей СЗЗ"][i],
                "headers": headers,
                "rows": table_rows,
                "widths": widths,
                "excel_widths": excel,
                "decimals": {value_col: 1} if has_value else {},
                "center": (len(headers) - 1,) if with_direction else (),
                "empty": self.tr("Участков в границах СЗЗ нет.") if i == 0 else
                self.tr("Участков в пределах {:g} м нет.").format(distance),
            })
        if coord_rows:
            export_tables.append({
                "title": self.tr("Таблица 3. Каталог координат характерных точек границы "
                                 "санитарно-защитной зоны ({})").format(coord_name),
                "sheet": "Координаты СЗЗ",
                "headers": [self.tr("№ точки"), self.tr("X, м"), self.tr("Y, м")],
                "rows": [[n, x, y] for n, x, y, _ in coord_rows],
                "widths": [20, 40, 40],
                "excel_widths": [10, 16, 16],
                "decimals": {1: 2, 2: 2},
                "after": self.tr("Площадь санитарно-защитной зоны — {} м² ({} га).").format(
                    _ru(round(coord_area)), _ru(round(coord_area / 10000, 4))),
            })
        xlsx = self.parameterAsFileOutput(parameters, self.XLSX, context)
        docx = self.parameterAsFileOutput(parameters, self.DOCX, context)
        if xlsx and self._save(feedback, xlsx, "Excel", export.write_xlsx, export_tables):
            results[self.XLSX] = xlsx
        if docx:
            notes = []
            if from_enterprise:
                notes.append(self.tr("Санитарно-защитная зона совпадает с границей предприятия; "
                                     "расстояния отсчитываются от границы предприятия."))
            elif zone_names:
                notes.append(self.tr("Санитарно-защитная зона: {}.").format("; ".join(zone_names)))
            notes.append(self.tr(
                "Участки, заходящие в СЗЗ частично, отнесены к таблице 1. Заход меньше {} м² или "
                "полосой уже допустимой погрешности границ ({}) считается расхождением границ, "
                "а не попаданием в СЗЗ. Участки самого предприятия в таблицы не включены. "
                "Расстояние — кратчайшее от участка до {}, м.").format(
                    _ru(classifier.min_overlap),
                    "; ".join("{} — {} м".format(core.DEPTH_NAMES[k], _ru(v))
                              for k, v in classifier.depths.items()),
                    zone_word))
            notes.append(self.tr("Источник сведений: слой «{}», дата составления: {}.").format(
                parcels.sourceName(), datetime.date.today().strftime("%d.%m.%Y")))
            title = self.tr("Реестр земельных участков в санитарно-защитной зоне")
            if title_object:
                title += " " + title_object
            if self._save(feedback, docx, "Word", export.write_docx, title, notes, export_tables):
                results[self.DOCX] = docx
        feedback.setProgress(100)
        if search_sink is not None:
            results[self.SEARCH] = search_id
        if results_points is not None:
            results[self.POINTS] = results_points
        if coord_rows:
            results["COORD_CRS_NAME"] = coord_name
        results.update({"INSIDE_COUNT": len(inside), "NEAR_COUNT": len(near),
                        "OWN_COUNT": len(own), "FROM_ENTERPRISE": from_enterprise})
        return results


def _ru(number):
    """Число для текста: «0,1», «1» — без лишних нулей, с запятой."""
    return "{:g}".format(number).replace(".", ",")


def _proj_params(proj):
    """Параметры строки PROJ: {ключ: значение или кортеж чисел}."""
    params = {}
    for token in (proj or "").split():
        if not token.startswith("+") or token == "+type=crs":
            continue
        key, _, value = token[1:].partition("=")
        try:
            params[key] = tuple(float(v) for v in value.split(",")) if value else ()
        except ValueError:
            params[key] = value
    return params


def _same_params(a, b):
    """Параметры совпадают с точностью до хвостов дробей: 3.40 после пересохранения СК пишет
    towgs84 «-0.219999999995» вместо «-0.22» — строки уже не равны, а СК та же."""
    if a.keys() != b.keys():
        return False
    for key, value in a.items():
        other = b[key]
        if isinstance(value, tuple) and isinstance(other, tuple):
            if len(value) != len(other) or any(abs(x - y) > 1e-6 for x, y in zip(value, other)):
                return False
        elif value != other:
            return False
    return True


def _msk_items(crs):
    """Записи набора МСК с теми же параметрами, что у СК (бывает несколько: МСК-01 = МСК-23 зона 1)."""
    params = _proj_params(crs.toProj())
    if not params:
        return []
    return [i for i in msk.items() if _same_params(_proj_params(i["proj"]), params)]
