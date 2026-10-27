"""Алгоритм «Реестр участков в СЗЗ»: участки в границах СЗЗ и ближайшие за ними."""

import datetime
import os
import re

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
    QgsSpatialIndex,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDistance,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from .. import core, export, msk, zouit

# msk.py и msk.json — копии из модуля project_utm_crs, правятся там; представляемся своим именем
msk.USER_AGENT = "QGIS plugin szz_parcels (https://github.com/Slider007/qgis-szz-parcels)"
# Тесты подменяют запрос к сервису адресов (Nominatim): функция (lon, lat) → (ответ, ошибка)
GEOCODE = None

INSIDE_ALIASES = {"num": "№ п/п", "cad_num": "Кадастровый номер", "category": "Категория земель",
                  "vri_doc": "ВРИ по документу", "normed": "Нормируемая территория",
                  "terr_zone": "Территориальная зона",
                  "szz_pct": "Доля участка в СЗЗ, %",
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
    SZZ_WIDTH = "SZZ_WIDTH"
    SMOOTH = "SMOOTH"
    SZZ_POINTS = "SZZ_POINTS"
    NORMED = "NORMED"
    NORMED_OUT = "NORMED_OUT"
    SZZ_OUT = "SZZ_OUT"
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
    ZOUIT_XML = "ZOUIT_XML"
    TERR_ZONES = "TERR_ZONES"
    EXTRACT = "EXTRACT"
    AUTHORITY = "AUTHORITY"
    START_DATE = "START_DATE"

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
            "Если слой СЗЗ не задан, СЗЗ — граница предприятия плюс заданный размер, м "
            "(0 — СЗЗ совпадает с границей, расстояния отсчитываются от неё). Граница предприятия — полигоны или замкнутые линии "
            "(например, из чертежа); все объекты слоя объединяются, как и все зоны слоя СЗЗ "
            "(чтобы взять часть — «только выделенные»).\n\n"
            "Участки самого предприятия (больше половины площади внутри его границы) в "
            "таблицы не входят. Участок, частично заходящий в СЗЗ, относится к первой таблице. "
            "Контуры с одним кадастровым номером (многоконтурный участок, повтор в выгрузке) "
            "дают одну строку.\n\n"
            "Расстояние — кратчайшее от участка до границы СЗЗ, на плоскости СК слоя участков "
            "(если он в градусах — в зоне UTM по месту).\n\n"
            "Нормируемые территории (жильё, сады, дачи, отдых, спорт, детские, образовательные и "
            "лечебные учреждения — СанПиН 2.2.1/2.1.1.1200-03, п. 5.1) отмечаются по ВРИ по "
            "документу; те, что в СЗЗ, — отдельным слоем.\n\n"
            "Таблицы можно сохранить в Excel и в Word (A4).")

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
            self.SZZ_WIDTH, self.tr("Без слоя СЗЗ: размер СЗЗ от границы предприятия, м "
                                    "(0 — СЗЗ по границе)"), 0.0, self.ENTERPRISE, False, 0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SMOOTH, self.tr("Сгладить «квадратную» СЗЗ из слоя: углы — дугой по размеру СЗЗ "
                                 "от границы предприятия"), False))
        self.addParameter(QgsProcessingParameterNumber(
            self.SZZ_POINTS, self.tr("Поворотных точек на контуре скруглённой СЗЗ (0 — без "
                                     "ограничения)"), Qgis.ProcessingNumberParameterType.Integer,
            core.SZZ_POINTS, False, 0, 10000))
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
        self.addParameter(QgsProcessingParameterBoolean(
            self.NORMED, self.tr("Отмечать нормируемые территории (жильё, сады, отдых, школы, "
                                 "больницы — по ВРИ)"), True))
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
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.TERR_ZONES, self.tr("Территориальные зоны ПЗЗ (например, с НСПД) — столбец в таблицах"),
            [Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SZZ_OUT, self.tr("СЗЗ, заданная размером"), Qgis.ProcessingSourceType.VectorPolygon,
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.NORMED_OUT, self.tr("Нормируемые территории в СЗЗ"),
            Qgis.ProcessingSourceType.VectorPolygon, optional=True, createByDefault=False))
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
        self.addParameter(QgsProcessingParameterFileDestination(
            self.ZOUIT_XML, self.tr("XML о границе СЗЗ для ЕГРН (interact_entry_boundaries)"),
            self.tr("XML (*.xml)"), optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFile(
            self.EXTRACT, self.tr("Выписка ЕГРН о земельном участке предприятия (XML) — адрес"),
            extension="xml", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.AUTHORITY, self.tr("Орган, принявший решение об установлении СЗЗ"), optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.START_DATE, self.tr("Дата начала действия СЗЗ (ГГГГ-ММ-ДД)"), optional=True))

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

    def _terr_zones(self, parameters, context, work_crs, feedback):
        """Функция «строка реестра → территориальные зоны участка» или None (слой не задан).
        Зоны, под которыми не меньше 5 % участка, по убыванию доли; «;» между ними."""
        source = self.parameterAsSource(parameters, self.TERR_ZONES, context)
        if source is None:
            return None
        field = core.find_field(source.fields().names(), core.TERR_ZONE_FIELDS)
        to_work = QgsCoordinateTransform(source.sourceCrs(), work_crs, context.transformContext())
        zones, index = [], QgsSpatialIndex()
        for feature in source.getFeatures():
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            geometry = QgsGeometry(geometry)
            geometry.transform(to_work)
            geometry = geometry.makeValid()
            name = core.text(feature[field]) if field else None
            zones.append((name or self.tr("зона без названия"), geometry))
            index.addFeature(len(zones) - 1, geometry.boundingBox())
        feedback.pushInfo(self.tr("Территориальных зон в слое: {} (поле названия — «{}»)").format(
            len(zones), field or "—"))

        def names(row):
            parcel = QgsGeometry.unaryUnion(row["work"]) if len(row["work"]) > 1 else row["work"][0]
            parcel = parcel.makeValid()
            area = parcel.area()
            shares = {}
            for i in index.intersects(parcel.boundingBox()):
                name, zone = zones[i]
                part = zone.intersection(parcel)
                if part is not None and not part.isEmpty() and area > 0:
                    shares[name] = shares.get(name, 0) + part.area() / area
            chosen = [n for n, v in sorted(shares.items(), key=lambda kv: -kv[1]) if v >= 0.05]
            return "; ".join(chosen)
        return names

    def _address(self, parameters, context, enterprise_src):
        """Адрес предприятия строкой: из выписки ЕГРН (XML), иначе из карточки НСПД в слое границы
        (поле readable_address — граница загружена «Найти на НСПД»)."""
        path = self.parameterAsFile(parameters, self.EXTRACT, context)
        extract = zouit.read_extract(path) if path else None
        if extract and extract.get("readable"):
            return extract["readable"]
        if "readable_address" in enterprise_src.fields().names():
            for feature in enterprise_src.getFeatures():
                if core.text(feature["readable_address"]):
                    return core.text(feature["readable_address"])
        return None

    def _zouit(self, parameters, context, feedback, path, outline, coord_name, rows, title_object):
        """XML о границе СЗЗ для ЕГРН. Путь или None (с предупреждением, почему нет)."""
        if outline is None:
            feedback.pushWarning(self.tr("XML для ЕГРН не составлен: нужны координаты СЗЗ в МСК "
                                         "(галочка «каталог координат» и МСК)."))
            return None
        extract_path = self.parameterAsFile(parameters, self.EXTRACT, context)
        extract = zouit.read_extract(extract_path) if extract_path else None
        if extract_path and extract is None:
            feedback.pushWarning(self.tr("В файле «{}» нет адреса участка: нужна выписка ЕГРН о "
                                         "земельном участке в XML.").format(os.path.basename(extract_path)))
        # кадастровый район — участка предприятия из выписки, иначе самый частый среди участков
        numbers = [extract["cad"]] if extract and extract.get("cad") else \
            [r["cad"] for r in rows.values() if r["cad"]]
        districts = [":".join(n.split(":")[:2]) for n in numbers if n.count(":") >= 2]
        if not districts:
            feedback.pushWarning(self.tr("XML для ЕГРН не составлен: не из чего взять кадастровый "
                                         "район — нет кадастровых номеров."))
            return None
        district = max(set(districts), key=districts.count)
        region = "{:02d}".format(int(district.split(":")[0]))
        item = next((i for i in msk.items() if msk.short_name(i) == coord_name), None) or \
            next((i for i in msk.items() if msk.short_name(i) in (coord_name or "").split(" / ")
                  and i["code"] == int(region)), None)
        if item is None:
            feedback.pushWarning(self.tr("XML для ЕГРН не составлен: система координат «{}» — не "
                                         "МСК из набора, а ЕГРН принимает координаты в МСК.").format(coord_name))
            return None
        location = extract["location"] if extract else None
        if not extract:
            feedback.pushWarning(self.tr("В XML для ЕГРН адрес — только код субъекта: укажите "
                                         "выписку ЕГРН (XML) участка предприятия, чтобы взять адрес."))
        date = self.parameterAsString(parameters, self.START_DATE, context).strip()
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            feedback.pushWarning(self.tr("Дата начала действия «{}» не в виде ГГГГ-ММ-ДД — в XML не "
                                         "указана.").format(date))
            date = ""
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            guid = zouit.write(
                path, core.szz_rings(outline), "{:02d}.{}".format(item["code"], item["zone"]),
                district, region, title_object,
                self.parameterAsString(parameters, self.AUTHORITY, context).strip(), date, location,
                _guid_from_name(path))
        except OSError:
            feedback.pushWarning(self.tr("XML «{}» записать не удалось: файл открыт или папка "
                                         "недоступна.").format(os.path.basename(path)))
            return None
        feedback.pushInfo(self.tr("XML для ЕГРН: {} (документ {})").format(path, guid))
        return path

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
        szz_width = 0.0 if szz_src is not None else \
            self.parameterAsDouble(parameters, self.SZZ_WIDTH, context)
        from_enterprise = szz_src is None and szz_width <= 0
        with_normed = self.parameterAsBoolean(parameters, self.NORMED, context)
        points_limit = self.parameterAsInt(parameters, self.SZZ_POINTS, context)
        smoothed = False
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
            elif self.parameterAsBoolean(parameters, self.SMOOTH, context):
                # «квадратная» СЗЗ (углы прямые): дуга по размеру от границы; у неравномерной
                # (разный размер по румбам) скругление по наименьшему урезало бы зону — не трогаем
                low, high = core.zone_width(enterprise, zone)
                if not zone.contains(enterprise) or low <= 0:
                    feedback.pushWarning(self.tr("СЗЗ не сглажена: она не охватывает предприятие."))
                elif high - low > max(0.1 * low, 1.0):
                    feedback.pushWarning(self.tr(
                        "СЗЗ не сглажена: размер разный, от {:.0f} до {:.0f} м от границы "
                        "предприятия, — скругление по наименьшему уменьшило бы СЗЗ.").format(low, high))
                else:
                    szz_width = round(low, 1)
                    zone = core.round_zone(enterprise, szz_width, points_limit)
                    smoothed = True
                    feedback.pushInfo(self.tr("СЗЗ сглажена: {:g} м от границы предприятия, "
                                              "точек — {}").format(szz_width, core.vertex_count(zone)))
        elif szz_width > 0:  # СЗЗ задана размером: граница предприятия + N м во все стороны
            zone = core.round_zone(enterprise, szz_width, points_limit)
            zone_names.append(self.tr("{:g} м от границы предприятия").format(szz_width))
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

        # СЗЗ, заданная размером, — слоем: для карты и легенды (слоя СЗЗ в проекте нет)
        results_szz = None
        if szz_width > 0 or smoothed:
            szz_fields = QgsFields()
            szz_fields.append(QgsField("width_m", QMetaType.Type.Double))
            szz_sink, szz_id = self.parameterAsSink(
                parameters, self.SZZ_OUT, context, szz_fields, Qgis.WkbType.MultiPolygon,
                parcels.sourceCrs())
            if szz_sink is not None:
                shape = QgsGeometry(classifier.zone)
                if work_crs != parcels.sourceCrs():
                    shape.transform(QgsCoordinateTransform(work_crs, parcels.sourceCrs(),
                                                           context.transformContext()))
                shape.convertToMultiType()
                szz_feature = QgsFeature(szz_fields)
                szz_feature.setGeometry(shape)
                szz_feature.setAttributes([szz_width])
                szz_sink.addFeature(szz_feature, QgsFeatureSink.Flag.FastInsert)
                if hasattr(szz_sink, "finalize"):
                    szz_sink.finalize()
                results_szz = szz_id

        # каталог координат границы СЗЗ (по выбору пользователя 2026-10-25: МСК по месту,
        # с северо-западной точки по часовой; таблица 3, лист Excel, слой точек, номера на карте)
        coord_rows, coord_name, coord_area, results_points = [], None, None, None
        outline = None
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
        terr = self._terr_zones(parameters, context, work_crs, feedback)

        def attributes(row):
            # пустые ячейки заполняются по просьбе пользователя (2026-09-29)
            feature = row["feature"]
            category = feature[category_field] if category_field else None
            vri = core.text(feature[vri_field]) if vri_field else None
            values = [row["cad"] or core.NO_VALUE, core.category_text(category),
                      vri or core.NO_VALUE]
            if with_normed:
                values.append(core.normed(vri))
            if terr is not None:
                values.append(terr(row) or core.NO_VALUE)
            return values

        def rhumb(row):
            parcel = row["work"][0] if len(row["work"]) == 1 else QgsGeometry.unaryUnion(row["work"])
            if parcel.isNull():  # контуры не сливаются (испорченная выгрузка) — просто вместе
                parcel = QgsGeometry.collectGeometry(row["work"])
            return core.direction(enterprise, parcel, from_boundary) or core.NO_VALUE

        def fields(last):
            result = QgsFields()
            result.append(QgsField("num", QMetaType.Type.Int))
            for name in ("cad_num", "category", "vri_doc") + (("normed",) if with_normed else ()) + \
                    (("terr_zone",) if terr is not None else ()):
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
        normed_rows = []  # нормируемые территории в СЗЗ: слой и перечень в Word
        normed_sink, normed_id = (None, None)
        if with_normed:
            normed_sink, normed_id = self.parameterAsSink(
                parameters, self.NORMED_OUT, context, inside_fields, Qgis.WkbType.MultiPolygon,
                parcels.sourceCrs())
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
                if key == self.INSIDE and with_normed and values[4] == core.NORMED_YES:
                    normed_rows.append(values)
                    if normed_sink is not None:
                        normed_sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
            if hasattr(sink, "finalize"):
                sink.finalize()
            names = INSIDE_ALIASES if key == self.INSIDE else near_aliases(from_enterprise)
            if context.willLoadLayerOnCompletion(dest_id):
                context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(
                    _Aliases.create(names))
            results[key] = dest_id
            tables.append(([names[f.name()] for f in layer_fields], table_rows))

        if normed_sink is not None:
            if hasattr(normed_sink, "finalize"):
                normed_sink.finalize()
            if context.willLoadLayerOnCompletion(normed_id):
                context.layerToLoadOnCompletionDetails(normed_id).setPostProcessor(
                    _Aliases.create(INSIDE_ALIASES))
            results[self.NORMED_OUT] = normed_id
        if with_normed:
            feedback.pushInfo(self.tr("Нормируемых территорий в СЗЗ: {}").format(len(normed_rows)))

        # 4. Файлы
        zone_word = self.tr("границы предприятия") if from_enterprise else self.tr("СЗЗ")
        titles = [self.tr("Таблица 1. Земельные участки в границах санитарно-защитной зоны"),
                  self.tr("Таблица 2. Земельные участки за границей {} на расстоянии до {:g} м")
                  .format(self.tr("предприятия") if from_enterprise
                          else self.tr("санитарно-защитной зоны"), distance)]
        export_tables = []
        for i, (headers, table_rows) in enumerate(tables):
            has_value = (i == 1) or share
            value_col = (4 + bool(with_normed) + (terr is not None)) if has_value else None
            # направление — 18: «предприятия» в шапке не рвётся по слогам; место — у ВРИ
            vri_w = (28 if with_direction else 32) - (14 if with_normed else 0)
            # «№ п/п» — 9: при 7 шапка переносилась на две строки
            widths = [9, 21, 22, vri_w] + ([14] if with_normed else []) + \
                ([22] if terr is not None else []) + \
                ([17] if has_value else []) + ([18] if with_direction else [])
            excel = [7, 22, 30, 50] + ([14] if with_normed else []) + \
                ([40] if terr is not None else []) + \
                ([16] if has_value else []) + ([14] if with_direction else [])
            center = ((4,) if with_normed else ()) + ((len(headers) - 1,) if with_direction else ())
            export_tables.append({
                "title": titles[i],
                "sheet": ["В границах СЗЗ", "За границей СЗЗ"][i],
                "headers": headers,
                "rows": table_rows,
                "widths": widths,
                "excel_widths": excel,
                "decimals": {value_col: 1} if has_value else {},
                "center": center,
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
        xml_path = self.parameterAsFileOutput(parameters, self.ZOUIT_XML, context)
        if xml_path:
            written = self._zouit(parameters, context, feedback, xml_path, outline, coord_name,
                                  rows, title_object)
            if written:
                results[self.ZOUIT_XML] = written
        if docx:
            notes = []
            if from_enterprise:
                notes.append(self.tr("Санитарно-защитная зона совпадает с границей предприятия; "
                                     "расстояния отсчитываются от границы предприятия."))
            elif zone_names:
                notes.append(self.tr("Санитарно-защитная зона: {}.").format("; ".join(zone_names)))
            if smoothed:
                notes.append(self.tr(
                    "Граница СЗЗ сглажена: {:g} м от границы предприятия, углы — дугой; хорды дуги "
                    "проведены снаружи, СЗЗ нигде не меньше {:g} м.").format(szz_width, szz_width))
            notes.append(self.tr(
                "Участки, заходящие в СЗЗ частично, отнесены к таблице 1. Заход меньше {} м² или "
                "полосой уже допустимой погрешности границ ({}) считается расхождением границ, "
                "а не попаданием в СЗЗ. Участки самого предприятия в таблицы не включены. "
                "Расстояние — кратчайшее от участка до {}, м.").format(
                    _ru(classifier.min_overlap),
                    "; ".join("{} — {} м".format(core.DEPTH_NAMES[k], _ru(v))
                              for k, v in classifier.depths.items()),
                    zone_word))
            if with_normed:
                note = self.tr(
                    "Нормируемые территории (СанПиН 2.2.1/2.1.1.1200-03, п. 5.1: жилая застройка, "
                    "сады, огороды, дачи, ЛПХ, зоны отдыха, курорты, санатории, спортивные "
                    "сооружения, детские, образовательные и лечебные учреждения) определены по ВРИ "
                    "по документу; «нет данных» — ВРИ не указан. ")
                if normed_rows:
                    note += self.tr("В границах СЗЗ — {}: {}.").format(
                        len(normed_rows), ", ".join(str(v[1]) for v in normed_rows))
                else:
                    note += self.tr("В границах СЗЗ нормируемых территорий не выявлено.")
                notes.append(note)
            address = self._address(parameters, context, enterprise_src)
            if address:
                notes.append(self.tr("Местоположение предприятия: {}.").format(address.rstrip(".")))
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
        if results_szz is not None:
            results[self.SZZ_OUT] = results_szz
            results["SZZ_SIZE"] = szz_width
            results["SMOOTHED"] = smoothed
        if coord_rows:
            results["COORD_CRS_NAME"] = coord_name
        results.update({"NORMED_COUNT": len(normed_rows), "INSIDE_COUNT": len(inside), "NEAR_COUNT": len(near),
                        "OWN_COUNT": len(own), "FROM_ENTERPRISE": from_enterprise})
        return results


def _guid_from_name(path):
    """guid из имени «interact_entry_boundaries_<guid>.xml» — имя и документ совпадают."""
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                      os.path.basename(path).lower())
    return match.group(1) if match else None


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
