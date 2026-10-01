"""Расчёт реестра участков в СЗЗ без интерфейса: общий для алгоритма и подсветки в окне."""

import re

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
)

# Доля площади участка внутри границы предприятия, с которой участок считается
# участком самого предприятия и в реестр не попадает
ENTERPRISE_SHARE = 50.0
# Заход участка в СЗЗ меньше этой площади — не попадание, а расхождение границ
MIN_OVERLAP_M2 = 0.01

# Чем заполняются пустые ячейки таблиц
NO_CATEGORY = "Категория не установлена"  # как в выписке ЕГРН
NO_VALUE = "—"
# Так ЕГРН пишет отсутствие категории: в таблице — тоже NO_CATEGORY (сравнение без регистра)
UNKNOWN_CATEGORY = {"категория не установлена", "категория не определена", "не установлена",
                    "не определена", "значение отсутствует", "неопределено", "не определено"}


def category_text(value):
    """Категория земель для таблицы: пустая или «не установлена» — NO_CATEGORY."""
    value = text(value)
    if value is None or value.lower() in UNKNOWN_CATEGORY:
        return NO_CATEGORY
    return value

# Поля выгрузки НСПД, импорта КПТ и частые названия (сравнение без регистра).
# В КПТ cadastral_number — номер квартала, не участка: в список не входит.
CAD_FIELDS = ["cad_num", "cad_number", "cadnum", "cn", "kn", "кадастровый номер",
              "кадастровый_номер", "кн"]
CATEGORY_FIELDS = ["land_record_category_type", "category", "category_type", "категория",
                   "категория земель"]
VRI_DOC_FIELDS = ["permitted_use_established_by_document", "permitted_doc", "util_by_doc", "vri_doc",
                  "ври по документу", "разрешенное использование по документу"]
ZONE_NAME_FIELDS = ["name_by_doc", "name", "наименование", "название"]


def find_field(names, candidates):
    """Первое поле слоя из списка известных названий, иначе None."""
    lower = {name.lower(): name for name in names}
    for candidate in candidates:
        if candidate in lower:
            return lower[candidate]
    return None


def text(value):
    """Значение атрибута как строка; NULL и пустое — None."""
    if value is None:
        return None
    if hasattr(value, "isNull") and value.isNull():
        return None
    value = str(value).strip()
    return value or None


def cad_sort_key(cad):
    """Кадастровые номера по порядку кварталов и участков: 23:47:0107010:9 раньше :10."""
    if not cad:
        return (1, [])
    return (0, [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", cad) if p])


def working_crs(parcels_crs, zone_geometry_wgs84=None):
    """СК расчёта в метрах: СК участков, если она в метрах, иначе UTM по месту СЗЗ."""
    if parcels_crs.isValid() and not parcels_crs.isGeographic():
        return parcels_crs
    lon = 0.0
    lat = 1.0
    if zone_geometry_wgs84 is not None and not zone_geometry_wgs84.isEmpty():
        centre = zone_geometry_wgs84.centroid().asPoint()
        lon, lat = centre.x(), centre.y()
    zone = int((lon + 180) // 6) + 1
    return QgsCoordinateReferenceSystem("EPSG:{}".format((32600 if lat >= 0 else 32700) + zone))


def _valid(geometry):
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    if not geometry.isGeosValid():
        geometry = geometry.makeValid()
    return geometry


def area_geometry(geometries):
    """Полигоны и замкнутые линии → одна площадная геометрия (объединение).

    Граница из чертежа часто приходит линиями: замкнутый контур считается площадью.
    Незамкнутая линия — ValueError: где у неё внутренняя сторона, неизвестно.
    """
    parts = []
    for geometry in geometries:
        geometry = _valid(geometry) if geometry is not None and \
            geometry.type() == Qgis.GeometryType.Polygon else geometry
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            continue
        if geometry.type() == Qgis.GeometryType.Polygon:
            parts.append(geometry)
        elif geometry.type() == Qgis.GeometryType.Line:
            for line in geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]:
                line = QgsGeometry(line.constGet().segmentize()) if line.constGet() else line
                points = line.asPolyline()
                if len(points) < 4 or points[0].distance(points[-1]) > 1e-6:
                    raise ValueError("граница не замкнута — соедините концы линии")
                parts.append(_valid(QgsGeometry.fromPolygonXY([points])))
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    union = QgsGeometry.unaryUnion(parts)
    return _valid(union)


class Classifier:
    """Делит участки на «в СЗЗ», «рядом» и «участки предприятия».

    Все геометрии — в одной СК в метрах (working_crs). Расстояние — кратчайшее
    от участка до СЗЗ на плоскости этой СК. Контуры одного участка (одинаковый
    кадастровый номер: многоконтурный участок, дубль в выгрузке) складываются
    в одну строку: add() по каждому контуру, затем result().
    """

    INSIDE, NEAR, ENTERPRISE, FAR = "inside", "near", "enterprise", "far"

    def __init__(self, enterprise, zone, distance, enterprise_share=ENTERPRISE_SHARE,
                 min_overlap=MIN_OVERLAP_M2):
        self.enterprise = enterprise
        self.zone = zone if zone is not None else enterprise
        self.distance = max(0.0, float(distance))
        self.enterprise_share = enterprise_share
        self.min_overlap = min_overlap
        self._zone_engine = QgsGeometry.createGeometryEngine(self.zone.constGet())
        self._zone_engine.prepareGeometry()
        self._ent_engine = QgsGeometry.createGeometryEngine(self.enterprise.constGet())
        self._ent_engine.prepareGeometry()

    def search_area(self):
        """Зона поиска: СЗЗ, расширенная на расстояние поиска."""
        if self.distance <= 0:
            return QgsGeometry(self.zone)
        return self.zone.buffer(self.distance, 16)

    def measure(self, geometry):
        """[площадь, в границе предприятия, в СЗЗ, расстояние до СЗЗ] или None (пустая)."""
        geometry = _valid(geometry)
        if geometry is None:
            return None
        area = geometry.area()
        if area <= 0:
            return None
        ent = zone = 0.0
        if self._ent_engine.intersects(geometry.constGet()):
            ent = geometry.intersection(self.enterprise).area()
        if self._zone_engine.intersects(geometry.constGet()):
            zone = geometry.intersection(self.zone).area()
        distance = 0.0 if zone > 0 else self._zone_engine.distance(geometry.constGet())
        return [area, ent, zone, distance]

    @staticmethod
    def add(total, measures):
        """Сложить меры двух контуров одного участка."""
        if total is None:
            return list(measures)
        return [total[0] + measures[0], total[1] + measures[1], total[2] + measures[2],
                min(total[3], measures[3])]

    def result(self, measures):
        """(вид, число): для INSIDE — доля участка в СЗЗ, %; для NEAR и FAR — расстояние, м."""
        area, ent, zone, distance = measures
        if 100.0 * ent / area >= self.enterprise_share:
            return self.ENTERPRISE, None
        if zone > self.min_overlap:
            return self.INSIDE, min(100.0, 100.0 * zone / area)
        if distance <= self.distance:
            return self.NEAR, distance
        return self.FAR, distance

    def classify(self, geometry):
        """Вид одного контура: (вид, число) или (None, None) для пустой геометрии."""
        measures = self.measure(geometry)
        if measures is None:
            return None, None
        return self.result(measures)


def transform_geometry(geometry, source_crs, target_crs, transform_context):
    if geometry is None or source_crs == target_crs:
        return geometry
    geometry = QgsGeometry(geometry)
    geometry.transform(QgsCoordinateTransform(source_crs, target_crs, transform_context))
    return geometry


def parcel_request(search_area, work_crs, parcels_crs, transform_context):
    """Запрос участков только в прямоугольнике зоны поиска (в СК слоя участков)."""
    rect = search_area.boundingBox()
    if work_crs != parcels_crs:
        rect = QgsCoordinateTransform(work_crs, parcels_crs, transform_context) \
            .transformBoundingBox(rect)
    rect.grow(1e-9 + 1e-6 * max(rect.width(), rect.height()))
    return QgsFeatureRequest().setFilterRect(rect)


def collect(features, cad_field, to_work, classifier, feedback=None, total=0):
    """Участки по кадастровому номеру: {ключ: {"cad", "feature", "geoms", "fids", "m"}}.

    to_work — QgsCoordinateTransform в СК расчёта или None. Контуры с одним номером
    складываются; участок без номера — отдельная строка. Второе значение — число
    объектов без геометрии.
    """
    rows = {}
    empty = 0
    for n, feature in enumerate(features):
        if feedback is not None:
            if feedback.isCanceled():
                return None, empty
            if total:
                feedback.setProgress(80.0 * n / total)
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            empty += 1
            continue
        work = QgsGeometry(geometry)
        if to_work is not None:
            work.transform(to_work)
        measures = classifier.measure(work)
        if measures is None:
            empty += 1
            continue
        cad = text(feature[cad_field]) if cad_field else None
        key = cad or "#{}".format(feature.id())
        row = rows.get(key)
        if row is None:
            row = rows[key] = {"cad": cad, "feature": feature, "geoms": [], "fids": [], "m": None}
        row["geoms"].append(geometry)
        row["fids"].append(feature.id())
        row["m"] = classifier.add(row["m"], measures)
    return rows, empty


def split(rows, classifier):
    """(в СЗЗ, рядом, участки предприятия): списки (строка, число), по порядку таблиц."""
    inside, near, own = [], [], []
    for row in rows.values():
        kind, value = classifier.result(row["m"])
        if kind == classifier.INSIDE:
            inside.append((row, value))
        elif kind == classifier.NEAR:
            near.append((row, value))
        elif kind == classifier.ENTERPRISE:
            own.append((row, value))
    inside.sort(key=lambda item: cad_sort_key(item[0]["cad"]))
    near.sort(key=lambda item: (round(item[1], 1), cad_sort_key(item[0]["cad"])))
    own.sort(key=lambda item: cad_sort_key(item[0]["cad"]))
    return inside, near, own
