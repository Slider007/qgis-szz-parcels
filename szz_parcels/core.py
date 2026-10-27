"""Расчёт реестра участков в СЗЗ без интерфейса: общий для алгоритма и подсветки в окне."""

import re

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
)

# Доля площади участка внутри границы предприятия, с которой участок считается
# участком самого предприятия и в реестр не попадает
ENTERPRISE_SHARE = 50.0
# Заход участка в СЗЗ меньше этой площади или уже допуска по категории земель — не попадание,
# а расхождение границ (решения пользователя 2026-10-24: «порог 0,1 м или даже 1 м²», допуски —
# по категориям земель, менять в дополнительных настройках)
MIN_OVERLAP_M2 = 1.0
MIN_DEPTH_M = 0.1
# Допуск по глубине захода, м — средняя квадратическая погрешность границ участка по требованиям
# Росреестра к точности (по категориям земель). Ключ — группа из depth_group().
DEPTHS = {
    "settlement": 0.1,  # земли населённых пунктов
    "garden": 0.2,      # сельхозземли под ЛПХ, сады, огороды, дачи, ИЖС, гаражи
    "industry": 0.5,    # земли промышленности, энергетики, транспорта, связи…
    "agri": 2.5,        # прочие сельхозземли
    "forest": 5.0,      # лесной и водный фонд, ООПТ, земли запаса
    "other": 0.1,       # категория не установлена — строже всего, чтобы не потерять заход
}
DEPTH_NAMES = {
    "settlement": "земли населённых пунктов",
    "garden": "сельхозземли под ЛПХ, сады, огороды, дачи, ИЖС",
    "industry": "земли промышленности",
    "agri": "прочие сельхозземли",
    "forest": "лесной и водный фонд, ООПТ, земли запаса",
    "other": "категория не установлена",
}


RHUMBS = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")


def rhumb(azimuth):
    """Румб (8 направлений) по азимуту в градусах от севера по часовой стрелке."""
    return RHUMBS[int(((azimuth % 360) + 22.5) // 45) % 8]


def direction(enterprise, parcel, from_boundary=False):
    """Румб от предприятия к участку (геометрии в СК расчёта, север — вверх по сетке).
    От центра — центроид предприятия → центроид участка; от границы — кратчайший отрезок от
    границы предприятия до участка (касается или внутри — от центра)."""
    if from_boundary:
        line = enterprise.shortestLine(parcel)
        points = line.asPolyline() if line and not line.isEmpty() else []
        if len(points) == 2 and points[0].distance(points[1]) > 0.01:
            return rhumb(points[0].azimuth(points[1]))
    start, end = _center(enterprise), _center(parcel)
    if start is None or end is None or start.distance(end) < 0.01:
        return None
    return rhumb(start.azimuth(end))


def _center(geometry):
    """Центр контура; у испорченного (выгрузка ЕГРН) — после makeValid, иначе None."""
    for g in (geometry, geometry.makeValid() if geometry is not None else None):
        if g is None or g.isNull() or g.isEmpty():
            continue
        center = g.centroid()
        if not center.isNull() and not center.isEmpty():
            return center.asPoint()
    return None


SZZ_POINTS = 20  # точек на контуре СЗЗ по умолчанию (пользователь, 2026-10-27: «15–20»)


def vertex_count(geometry):
    """Число поворотных точек полигона (без замыкающих)."""
    polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    return sum(max(len(ring) - 1, 0) for polygon in polygons for ring in polygon)


def round_zone(enterprise, width, points=SZZ_POINTS):
    """СЗЗ — граница предприятия + width м, углы скруглены дугой, всего около points точек.

    Дуга с малым числом точек — ломаная: её хорды ставятся снаружи настоящей дуги, чтобы
    СЗЗ нигде не была меньше width. Подбирается допуск t: буфер на width + t, упрощённый
    Дугласом — Пейкером с допуском t (отступ внутрь не больше t), — самый точный, у которого
    точек не больше points. points = 0 — без ограничения (гладкий буфер)."""
    exact = enterprise.buffer(width, 32)
    if points <= 0 or vertex_count(exact) <= points:
        return exact
    low, high = 0.0, width
    best = None
    for _ in range(40):
        t = (low + high) / 2
        # +2 см: хорды самого буфера и округление координат до 0,01 м — тоже внутрь
        shape = enterprise.buffer(width + t + 0.02, 32).simplify(t)
        if not shape.isEmpty() and vertex_count(shape) <= points:
            best, high = shape, t
        else:
            low = t
    if best is None:  # точек меньше, чем углов у предприятия, — сколько вышло при допуске = width
        best = enterprise.buffer(2 * width, 32).simplify(width)
    return best.makeValid() if not best.isGeosValid() else best


def zone_width(enterprise, zone):
    """(наименьший, наибольший) отступ СЗЗ от предприятия по серединам сторон контура СЗЗ.
    Для «квадратной» СЗЗ они равны; разные — СЗЗ неравномерная (по румбам)."""
    polygons = zone.asMultiPolygon() if zone.isMultipart() else [zone.asPolygon()]
    sizes = []
    for polygon in polygons:
        ring = polygon[0] if polygon else []
        for a, b in zip(ring, ring[1:]):
            middle = QgsGeometry.fromPointXY(QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))
            sizes.append(enterprise.distance(middle))
    return (min(sizes), max(sizes)) if sizes else (0.0, 0.0)


def depth_group(category, vri=None):
    """Группа допуска по категории земель (и ВРИ — для сельхозземель)."""
    c = (text(category) or "").lower().replace("ё", "е")
    v = (text(vri) or "").lower()
    if "населенн" in c:
        return "settlement"
    if "сельскохозяйствен" in c:
        return "garden" if re.search(r"садов|огород|подсобн|лпх|дач|жилищ|гараж", v) else "agri"
    if "промышленн" in c:
        return "industry"
    if any(w in c for w in ("особо охраняем", "лесного фонда", "водного фонда", "запаса")):
        return "forest"
    return "other"

# Нормируемые территории (СанПиН 2.2.1/2.1.1.1200-03, п. 5.1; перечень утвердил пользователь
# 2026-10-25): жильё и ИЖС, сады, огороды, дачи, ЛПХ, отдых, рекреация, курорты, санатории, спорт,
# детские и образовательные учреждения, больницы и поликлиники. Признак — по ВРИ по документу.
NORMED = re.compile(  # \b: «электропередачи», «транспорт», «парковка» — не дача, спорт и парк
    r"\b(жил(ой|ого|ых|ая|ые|ищн\w* строит|\w* застрой|\w* дом)|ижс|индивидуальн\w* строит|многоквартир|малоэтажн|"
    r"среднеэтажн|многоэтажн|блокированн|усадебн|приусадебн|садов|огород|дач|подсобн\w* хозяйств|"
    r"лпх|отдых|рекреац|курорт|санатор|туризм|спорт|физкульт|детск|дошкольн|школ|образован|"
    r"просвещен|воспитан|здравоохран|больниц|поликлин|амбулатор|лечебн|медицин|оздоров|"
    r"парк(?!ов)|сквер|пляж|лагер)")
NOT_NORMED = re.compile(r"жилищно-коммунальн\w*|гараж\w*|нежил\w*")
NORMED_YES, NORMED_NO, NORMED_UNKNOWN = "да", "нет", "нет данных"


def normed(vri):
    """Нормируемая ли территория по ВРИ по документу: «да», «нет» или «нет данных»."""
    value = text(vri)
    if value is None or value.lower() in UNKNOWN_CATEGORY:
        return NORMED_UNKNOWN
    value = NOT_NORMED.sub(" ", value.lower().replace("ё", "е"))
    return NORMED_YES if NORMED.search(value) else NORMED_NO


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

# Название территориальной зоны: НСПД (name_by_doc), прочие выгрузки
TERR_ZONE_FIELDS = ["name_by_doc", "name", "наименование", "zone_name", "название", "descr"]

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


CAD_NUMBER = re.compile(r"\d{1,2}:\d{1,2}:\d{6,7}:\d+")


def cad_numbers(value):
    """Кадастровые номера из строки («23:43:0000000:12, 23:43:0000000:13») по порядку, без повторов."""
    result = []
    for number in CAD_NUMBER.findall(value or ""):
        if number not in result:
            result.append(number)
    return result


def true_metres(crs):
    """Метры СК — настоящие: не градусы и не Меркатор (в EPSG:3857 «метр» на 56° с. ш. длиннее
    настоящего в 1,8 раза: «500 м» поиска были бы 280 м, площадь захода — втрое больше)."""
    return crs.isValid() and not crs.isGeographic() and "+proj=merc" not in crs.toProj()


def working_crs(parcels_crs, zone_geometry_wgs84=None):
    """СК расчёта в метрах: СК участков, если её метры настоящие, иначе UTM по месту СЗЗ."""
    if true_metres(parcels_crs):
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
                 min_overlap=MIN_OVERLAP_M2, min_depth=MIN_DEPTH_M, depths=None):
        self.enterprise = enterprise
        self.zone = zone if zone is not None else enterprise
        self.distance = max(0.0, float(distance))
        self.enterprise_share = enterprise_share
        self.min_overlap = min_overlap
        self.min_depth = min_depth  # без допуска по категории (контур границы, СЗЗ)
        self.depths = dict(DEPTHS if depths is None else depths)
        self._zone_engine = QgsGeometry.createGeometryEngine(self.zone.constGet())
        self._zone_engine.prepareGeometry()
        self._ent_engine = QgsGeometry.createGeometryEngine(self.enterprise.constGet())
        self._ent_engine.prepareGeometry()

    def search_area(self):
        """Зона поиска: СЗЗ, расширенная на расстояние поиска."""
        if self.distance <= 0:
            return QgsGeometry(self.zone)
        return self.zone.buffer(self.distance, 16)

    def depth_for(self, category=None, vri=None):
        """Допуск по глубине захода для участка по его категории земель (и ВРИ)."""
        return self.depths.get(depth_group(category, vri), self.min_depth)

    def measure(self, geometry, depth=None):
        """[площадь, в границе предприятия, в СЗЗ, расстояние до СЗЗ] или None (пустая).
        depth — допуск по глубине захода, м (по умолчанию min_depth)."""
        depth = self.min_depth if depth is None else depth
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
            overlap = geometry.intersection(self.zone)
            zone = overlap.area()
            # полоска уже min_depth — расхождение границ: при отрицательном буфере исчезает
            if zone > 0 and depth > 0 and overlap.buffer(-depth / 2, 4).isEmpty():
                zone = 0.0
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
        if zone >= self.min_overlap and zone > 0:
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


def collect(features, cad_field, to_work, classifier, feedback=None, total=0,
            category_field=None, vri_field=None):
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
        depth = classifier.depth_for(feature[category_field] if category_field else None,
                                     feature[vri_field] if vri_field else None)
        measures = classifier.measure(work, depth)
        if measures is None:
            empty += 1
            continue
        cad = text(feature[cad_field]) if cad_field else None
        key = cad or "#{}".format(feature.id())
        row = rows.get(key)
        if row is None:
            row = rows[key] = {"cad": cad, "feature": feature, "geoms": [], "work": [], "fids": [],
                               "m": None}
        row["geoms"].append(geometry)
        row["work"].append(work)
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


def _ring_points(ring, clockwise):
    """Вершины контура без повтора последней, по нужному обходу, с северо-западной первой."""
    points = [QgsPointXY(p) for p in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    clean = []
    for p in points:  # подряд одинаковые вершины — одна точка
        if not clean or p.distance(clean[-1]) > 0.001:
            clean.append(p)
    if len(clean) > 1 and clean[0].distance(clean[-1]) <= 0.001:
        clean.pop()
    # знак площади (формула шнурования): > 0 — против часовой при оси Y на север
    area = sum(a.x() * b.y() - b.x() * a.y() for a, b in zip(clean, clean[1:] + clean[:1]))
    if (area > 0) == clockwise:
        clean.reverse()
    # северо-западная: дальше всех по направлению на северо-запад (−x + y)
    start = max(range(len(clean)), key=lambda i: (clean[i].y() - clean[i].x(), clean[i].y()))
    return clean[start:] + clean[:start]


def szz_rings(geometry):
    """Контуры СЗЗ по полигонам: [[[(№, X на север, Y на восток), …] по кольцам] по полигонам].

    Внешний контур — по часовой, внутренний — против, каждый с северо-западной вершины;
    в конце кольца повторяется его первая точка. Номера сквозные. Геометрия — в той СК,
    в которой нужны координаты (МСК)."""
    result = []
    number = 0
    polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    for polygon in polygons:
        rings = []
        for index, ring in enumerate(polygon):
            points = _ring_points(ring, clockwise=(index == 0))
            rows = []
            for p in points:
                number += 1
                rows.append((number, round(p.y(), 2), round(p.x(), 2)))
            if rows:
                rows.append(rows[0])
                rings.append(rows)
        result.append(rings)
    return result


def szz_points(geometry):
    """Каталог точек границы СЗЗ: [(№, X на север, Y на восток, № контура)] — см. szz_rings."""
    rows = []
    contour = 0
    for rings in szz_rings(geometry):
        for ring in rings:
            contour += 1
            rows.extend((n, x, y, contour) for n, x, y in ring)
    return rows
