"""Проверки модуля без интерфейса QGIS. Запуск: tests/run_tests.sh

Настройки QGIS уводятся во временный профиль tests/_profile.

Схема тестовых данных (EPSG:32637, метры от точки X0, Y0):
предприятие — квадрат 0..100; СЗЗ — квадрат −50..150 (50 м от границы).
"""

import os
import shutil
import sys
import unittest
import warnings
import zipfile
from xml.etree import ElementTree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_profile")
OUT = os.path.join(HERE, "_out", "test")  # tests/_out/check — отчёт полной проверки, не трогать
shutil.rmtree(PROFILE, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
ORG = "szz-parcels-tests"
QCoreApplication.setOrganizationName(ORG)
QCoreApplication.setApplicationName(ORG)

import json  # noqa: E402
import time  # noqa: E402

from qgis.core import (  # noqa: E402
    NULL,
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingFeatureSourceDefinition,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)

from qgis.gui import QgsMapCanvas, QgsRubberBand  # noqa: E402
from qgis.PyQt import sip  # noqa: E402
from qgis.PyQt.QtCore import QEvent, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMenu, QToolBar  # noqa: E402

app = QgsApplication([], True, PROFILE)
if os.environ.get("QGIS_PREFIX_PATH"):
    app.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
app.initQgis()
# initQgis() переносит настройки в профиль default организации: возвращаем во временный
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
assert QSettings().fileName().startswith(PROFILE), QSettings().fileName()

# Processing из поставки QGIS
for path in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    plugins = os.path.join(path, "plugins")
    if os.path.isdir(os.path.join(plugins, "processing")) and plugins not in sys.path:
        sys.path.append(plugins)
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    import szz_parcels  # noqa: E402,F401
    from szz_parcels import core, export, nspd  # noqa: E402
    from szz_parcels import plugin as plugin_module  # noqa: E402,F401
    from szz_parcels.processing import registry_algorithm  # noqa: E402,F401
    from szz_parcels.processing.provider import SzzParcelsProvider  # noqa: E402

PROVIDER = SzzParcelsProvider()
QgsApplication.processingRegistry().addProvider(PROVIDER)
ALG = "szzparcels:registry"
CRS = "EPSG:32637"
X0, Y0 = 400000.0, 5000000.0


def rect(x1, y1, x2, y2):
    return QgsGeometry.fromRect(_r(x1, y1, x2, y2))


def _r(x1, y1, x2, y2):
    from qgis.core import QgsRectangle
    return QgsRectangle(X0 + x1, Y0 + y1, X0 + x2, Y0 + y2)


def ring(x1, y1, x2, y2, closed=True):
    from qgis.core import QgsPointXY
    points = [QgsPointXY(X0 + x, Y0 + y) for x, y in
              ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
    if closed:
        points.append(points[0])
    return QgsGeometry.fromPolylineXY(points)


def layer(kind, rows, fields="", crs=CRS, name="слой"):
    """rows — [(геометрия, [значения])]."""
    lyr = QgsVectorLayer("{}?crs={}{}".format(kind, crs, fields), name, "memory")
    features = []
    for geometry, values in rows:
        feature = QgsFeature(lyr.fields())
        if geometry is not None:
            feature.setGeometry(geometry)
        feature.setAttributes(values)
        features.append(feature)
    lyr.dataProvider().addFeatures(features)
    return lyr


PARCEL_FIELDS = ("&field=cad_num:string&field=land_record_category_type:string"
                 "&field=permitted_use_established_by_document:string")
# кадастровый номер → прямоугольник
PARCELS = [
    ("23:47:0107010:9", (10, 10, 90, 90), "Земли промышленности", "Под ПС"),       # предприятие
    ("23:47:0107010:10", (110, 0, 140, 30), "Земли населенных пунктов", "ИЖС"),   # в СЗЗ, 100 %
    ("23:47:0107010:10", (110, 0, 140, 30), "Земли населенных пунктов", "ИЖС"),   # повтор
    ("23:47:0107010:2", (140, 50, 160, 70), "Земли населенных пунктов", None),    # в СЗЗ на 50 %
    ("23:47:0107010:3", (170, 0, 190, 20), None, "ЛПХ"),                          # 20 м от СЗЗ
    ("23:47:0107010:4", (300, 0, 320, 20), "Земли населенных пунктов", "ИЖС"),   # 150 м — далеко
    ("23:47:0107010:5", (150, 100, 170, 120), "Земли населенных пунктов", "ИЖС"),  # касается
    ("23:47:0107010:6", (-80, 0, -60, 20), "Земли с/х назначения", "Пашня"),     # контур 1: 10 м
    ("23:47:0107010:6", (-95, 30, -85, 40), "Земли с/х назначения", "Пашня"),    # контур 2: 35 м
    ("23:47:0107010:7", (70, -35, 100, 15), "Земли населенных пунктов", "Склад"),  # 30 % в предприятии
    ("23:47:0107010:8", (149.99999, 0, 160, 10), None, None),                    # заход 0,0001 м²
]


def parcels_layer(crs=CRS, extra_empty=True):
    rows = [(rect(*box), [cad, cat, vri]) for cad, box, cat, vri in PARCELS]
    if extra_empty:
        rows.append((None, ["23:47:0107010:99", None, None]))
    lyr = layer("Polygon", rows, PARCEL_FIELDS, name="Земельные участки из ЕГРН")
    if crs != CRS:
        tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem(CRS),
                                    QgsCoordinateReferenceSystem(crs), QgsProject.instance())
        moved = []
        for feature in lyr.getFeatures():
            geometry = feature.geometry()
            if not geometry.isNull():
                geometry = QgsGeometry(geometry)
                geometry.transform(tr)
            moved.append((None if feature.geometry().isNull() else geometry, feature.attributes()))
        lyr = layer("Polygon", moved, PARCEL_FIELDS, crs=crs, name="Участки")
    return lyr


def enterprise_layer():
    return layer("Polygon", [(rect(0, 0, 100, 100), [])], name="Граница предприятия")


SZZ_FIELDS = "&field=name_by_doc:string"


def szz_layer(crs=CRS):
    lyr = layer("Polygon", [(rect(-50, -50, 150, 150), ["СЗЗ ПС"])], SZZ_FIELDS, name="СЗЗ")
    if crs != CRS:
        tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem(CRS),
                                    QgsCoordinateReferenceSystem(crs), QgsProject.instance())
        geometry = QgsGeometry(next(lyr.getFeatures()).geometry())
        geometry.transform(tr)
        lyr = layer("Polygon", [(geometry, ["СЗЗ ПС"])], SZZ_FIELDS, crs=crs, name="СЗЗ")
    return lyr


class Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.warnings = []
        self.infos = []
        self.progress = []
        self.progressChanged.connect(self.progress.append)

    def pushWarning(self, text):
        self.warnings.append(text)

    def pushInfo(self, text):
        self.infos.append(text)


def run(parcels=None, enterprise=None, szz=None, distance=100.0, **extra):
    params = {
        "PARCELS": parcels or parcels_layer(),
        "ENTERPRISE": enterprise or enterprise_layer(),
        "DISTANCE": distance,
        "INSIDE": "memory:",
        "NEAR": "memory:",
    }
    if szz is not False:
        params["SZZ"] = szz or szz_layer()
    params.update(extra)
    feedback = Feedback()
    result = processing.run(ALG, params, feedback=feedback)
    return result, feedback


def table(lyr, value_field):
    names = lyr.fields().names()
    rows = []
    for feature in lyr.getFeatures():
        value = feature[value_field] if value_field in names else None
        rows.append((feature["num"], feature["cad_num"],
                     None if value is None or value == NULL else round(value, 1)))
    return sorted(rows)


class CoreTest(unittest.TestCase):
    def test_find_field(self):
        self.assertEqual(core.find_field(["fid", "CAD_NUM"], core.CAD_FIELDS), "CAD_NUM")
        self.assertIsNone(core.find_field(["fid"], core.CAD_FIELDS))

    def test_cad_sort(self):
        cads = ["23:47:0107010:10", "23:47:0107002:5", "23:47:0107010:9", None]
        self.assertEqual(sorted(cads, key=core.cad_sort_key),
                         ["23:47:0107002:5", "23:47:0107010:9", "23:47:0107010:10", None])

    def test_text(self):
        self.assertIsNone(core.text(NULL))
        self.assertIsNone(core.text("  "))
        self.assertEqual(core.text(" ИЖС "), "ИЖС")

    def test_closed_lines_make_area(self):
        area = core.area_geometry([ring(0, 0, 100, 100), ring(200, 0, 210, 10)])
        self.assertAlmostEqual(area.area(), 100 * 100 + 10 * 10, 3)

    def test_unclosed_line_refused(self):
        with self.assertRaises(ValueError):
            core.area_geometry([ring(0, 0, 100, 100, closed=False)])

    def test_working_crs(self):
        self.assertEqual(core.working_crs(QgsCoordinateReferenceSystem(CRS)).authid(), CRS)
        geo = QgsGeometry.fromWkt("POINT(37.7 44.7)")
        self.assertEqual(core.working_crs(QgsCoordinateReferenceSystem("EPSG:4326"), geo).authid(),
                         "EPSG:32637")


class RegistryTest(unittest.TestCase):
    def test_szz_layer(self):
        result, feedback = run()
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        near = QgsProcessingContextless.layer(result["NEAR"])
        self.assertEqual(table(inside, "szz_pct"), [
            (1, "23:47:0107010:2", 50.0),
            (2, "23:47:0107010:7", 100.0),
            (3, "23:47:0107010:10", 100.0),
        ])
        self.assertEqual(table(near, "dist_m"), [
            (1, "23:47:0107010:5", 0.0),
            (2, "23:47:0107010:8", 0.0),
            (3, "23:47:0107010:6", 10.0),
            (4, "23:47:0107010:3", 20.0),
        ])
        self.assertEqual(result["OWN_COUNT"], 1)
        self.assertTrue(any("23:47:0107010:9" in text for text in feedback.infos))
        # участок без геометрии (:99) в зону поиска не попадает и расчёт не ломает
        # атрибуты и заголовки
        first = next(f for f in inside.getFeatures() if f["cad_num"] == "23:47:0107010:10")
        self.assertEqual((first["category"], first["vri_doc"]), ("Земли населенных пунктов", "ИЖС"))
        # два контура участка :6 — одна строка с геометрией обоих
        six = next(f for f in near.getFeatures() if f["cad_num"] == "23:47:0107010:6")
        self.assertAlmostEqual(six.geometry().area(), 400 + 100, 3)

    def test_empty_values_filled(self):
        """Нет категории — «Категория не установлена», в остальных пустых — прочерк."""
        docx = os.path.join(OUT, "пустые.docx")
        xlsx = os.path.join(OUT, "пустые.xlsx")
        result, _ = run(DOCX=docx, XLSX=xlsx)
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        near = QgsProcessingContextless.layer(result["NEAR"])
        rows = {f["cad_num"]: (f["category"], f["vri_doc"])
                for lyr in (inside, near) for f in lyr.getFeatures()}
        self.assertEqual(rows["23:47:0107010:2"], ("Земли населенных пунктов", "—"))
        self.assertEqual(rows["23:47:0107010:3"], ("Категория не установлена", "ЛПХ"))
        self.assertEqual(rows["23:47:0107010:8"], ("Категория не установлена", "—"))
        with zipfile.ZipFile(docx) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertEqual(xml.count("Категория не установлена"), 2)
        book = QgsVectorLayer(xlsx + "|layername=За границей СЗЗ", "x", "ogr")
        values = [f.attributes()[2:4] for f in book.getFeatures()]
        self.assertIn(["Категория не установлена", "—"], values)
        # «нет значения» другими словами — тоже «Категория не установлена», как в выписке
        worded = layer("Polygon", [
            (rect(110, 0, 120, 10), ["1:1:1:1", "категория не определена", "ИЖС"]),
            (rect(120, 0, 130, 10), ["1:1:1:2", "значение отсутствует", "ИЖС"]),
            (rect(130, 0, 140, 10), ["1:1:1:3", "Земли населенных пунктов", "ИЖС"])],
            PARCEL_FIELDS)
        result, _ = run(parcels=worded)
        self.assertEqual(
            sorted(f["category"] for f in
                   QgsProcessingContextless.layer(result["INSIDE"]).getFeatures()),
            ["Земли населенных пунктов", "Категория не установлена", "Категория не установлена"])
        # поля категории в слое нет вовсе — то же
        bare = layer("Polygon", [(rect(110, 0, 140, 30), ["1:1:1:1"])], "&field=cad_num:string")
        result, _ = run(parcels=bare)
        feature = next(QgsProcessingContextless.layer(result["INSIDE"]).getFeatures())
        self.assertEqual((feature["category"], feature["vri_doc"]),
                         ("Категория не установлена", "—"))


    def test_unwritable_file_keeps_layers(self):
        """Excel нельзя записать (открыт, путь занят): слои и Word всё равно есть, ошибка — по-русски."""
        busy = os.path.join(OUT, "занят.xlsx")
        os.makedirs(busy, exist_ok=True)
        docx = os.path.join(OUT, "при_занятом.docx")
        result, feedback = run(XLSX=busy, DOCX=docx)
        self.assertEqual(result["INSIDE_COUNT"], 3)
        self.assertIsNotNone(QgsProcessingContextless.layer(result["INSIDE"]))
        self.assertTrue(os.path.isfile(docx))
        self.assertNotIn("XLSX", result)
        text = " ".join(feedback.warnings)
        self.assertIn("Не удалось сохранить", text)
        self.assertIn("занят.xlsx", text)
        self.assertNotIn("Errno", text)

    def test_wrong_field_message_says_what_to_do(self):
        with self.assertRaisesRegex(Exception, "нет поля «нет_такого».*выберите"):
            run(CAD_FIELD="нет_такого")

    def test_progress_follows_selected_parcels(self):
        """Ход считается от участков в зоне поиска, а не от всего слоя."""
        rows = [(rect(*box), [cad, cat, vri]) for cad, box, cat, vri in PARCELS]
        rows += [(rect(5000 + 30 * i, 0, 5010 + 30 * i, 10), ["1:1:1:%d" % i, None, None])
                 for i in range(300)]
        big = layer("Polygon", rows, PARCEL_FIELDS)
        _, feedback = run(parcels=big)
        before_end = [p for p in feedback.progress if p < 100]
        self.assertGreaterEqual(max(before_end), 70)

    def test_parcels_in_web_mercator_measured_in_true_metres(self):
        """Слой участков в EPSG:3857: «метры» Меркатора на 45° длиннее настоящих в 1,4 раза —
        расчёт должен идти в UTM, иначе «100 м» — это 70 м, а заход в СЗЗ завышен вдвое."""
        result, _ = run(parcels=parcels_layer("EPSG:3857"), szz=szz_layer())
        near = QgsProcessingContextless.layer(result["NEAR"])
        dist = {f["cad_num"]: f["dist_m"] for f in near.getFeatures()}
        self.assertAlmostEqual(dist["23:47:0107010:3"], 20.0, delta=0.5)
        self.assertAlmostEqual(dist["23:47:0107010:6"], 10.0, delta=0.5)
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        share = {f["cad_num"]: f["szz_pct"] for f in inside.getFeatures()}
        self.assertAlmostEqual(share["23:47:0107010:2"], 50.0, delta=0.5)

    def test_micro_overlaps_ignored(self):
        """Расхождение границ — не заход в СЗЗ: меньше 1 м² или полоска уже 0,1 м."""
        rows = [
            (rect(149.95, 0, 170, 100), ["1:1:1:1", None, None]),    # полоска 5 см × 100 м = 5 м²
            (rect(149.3, 0, 170, 1.3), ["1:1:1:2", None, None]),     # 0,7 × 1,3 = 0,91 м², глубоко
            (rect(148, 20, 170, 22), ["1:1:1:3", None, None]),       # 2 × 2 = 4 м², 2 м вглубь
        ]
        mixed = layer("Polygon", rows, PARCEL_FIELDS)
        result, _ = run(parcels=mixed)
        inside = [f["cad_num"] for f in QgsProcessingContextless.layer(result["INSIDE"]).getFeatures()]
        near = {f["cad_num"]: f["dist_m"] for f in QgsProcessingContextless.layer(result["NEAR"]).getFeatures()}
        self.assertEqual(inside, ["1:1:1:3"])
        self.assertEqual(near, {"1:1:1:1": 0.0, "1:1:1:2": 0.0})

    def test_depth_by_land_category(self):
        """Допуск расхождения границ — по категории земель: полоска 0,3 м у населённого пункта —
        заход в СЗЗ, у промышленности (0,5 м) — нет; 3 м у лесного фонда (5 м) — нет."""
        cats = [
            ("1:1:1:1", "Земли населенных пунктов", "ИЖС"),
            ("1:1:1:2", "Земли промышленности, энергетики, транспорта", "Склады"),
            ("1:1:1:3", "Земли лесного фонда", None),
            ("1:1:1:4", "Земли сельскохозяйственного назначения", "Для ведения личного подсобного хозяйства"),
            ("1:1:1:5", "Земли сельскохозяйственного назначения", "Пашня"),
        ]
        widths = {"1:1:1:1": 0.3, "1:1:1:2": 0.3, "1:1:1:3": 3.0, "1:1:1:4": 0.3, "1:1:1:5": 2.0}
        rows = []
        for n, (cad, cat, vri) in enumerate(cats):
            y = 20 * n
            rows.append((rect(150 - widths[cad], y, 160, y + 10), [cad, cat, vri]))
        lyr = layer("Polygon", rows, PARCEL_FIELDS)
        result, _ = run(parcels=lyr)
        inside = sorted(f["cad_num"] for f in QgsProcessingContextless.layer(result["INSIDE"]).getFeatures())
        # 0,3 м: населённый пункт (0,1) и ЛПХ (0,2) — заход; промышленность (0,5) — нет;
        # 3 м у леса (5 м) — нет; 2 м у пашни (2,5 м) — нет
        self.assertEqual(inside, ["1:1:1:1", "1:1:1:4"])
        # допуски меняются параметрами
        result, _ = run(parcels=lyr, DEPTH_INDUSTRY=0.2, DEPTH_FOREST=1.0)
        inside = sorted(f["cad_num"] for f in QgsProcessingContextless.layer(result["INSIDE"]).getFeatures())
        self.assertEqual(inside, ["1:1:1:1", "1:1:1:2", "1:1:1:3", "1:1:1:4"])

    def test_depth_group(self):
        g = core.depth_group
        self.assertEqual(g("Земли населённых пунктов", None), "settlement")
        self.assertEqual(g("Земли сельскохозяйственного назначения", "для садоводства"), "garden")
        self.assertEqual(g("Земли сельскохозяйственного назначения", "Пашня"), "agri")
        self.assertEqual(g("Земли промышленности, энергетики, транспорта, связи", None), "industry")
        self.assertEqual(g("Земли особо охраняемых территорий и объектов", None), "forest")
        self.assertEqual(g("Земли водного фонда", None), "forest")
        self.assertEqual(g("Земли запаса", None), "forest")
        self.assertEqual(g("Категория не установлена", None), "other")
        self.assertEqual(g(None, None), "other")

    def test_rhumb(self):
        r = core.rhumb
        self.assertEqual([r(a) for a in (0, 22, 23, 45, 90, 135, 180, 225, 270, 315, 337, 338, -90)],
                         ["С", "С", "СВ", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ", "СЗ", "С", "З"])

    def test_direction_column(self):
        """Румб от центра предприятия (50; 50) и от ближайшей точки его границы."""
        result, _ = run(szz=False)
        near = QgsProcessingContextless.layer(result["NEAR"])
        by_cad = {f["cad_num"]: f["dir"] for f in near.getFeatures()}
        # :5 (150..170; 100..120): от центра — 61° (СВ); :10 (110..140; 0..30): 115° (ЮВ)
        self.assertEqual(by_cad["23:47:0107010:5"], "СВ")
        self.assertEqual(by_cad["23:47:0107010:10"], "ЮВ")
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        self.assertIn("dir", inside.fields().names())
        # от границы: ближайшая точка к :5 — угол (100; 100), отрезок на восток
        result, _ = run(szz=False, DIRECTION_FROM=1)
        near = QgsProcessingContextless.layer(result["NEAR"])
        self.assertEqual({f["cad_num"]: f["dir"] for f in near.getFeatures()}["23:47:0107010:5"], "В")
        # без направления — столбца нет
        result, _ = run(DIRECTION=False)
        self.assertNotIn("dir", QgsProcessingContextless.layer(result["NEAR"]).fields().names())

    def test_direction_in_files(self):
        xlsx = os.path.join(OUT, "румбы.xlsx")
        run(szz=False, XLSX=xlsx)
        near = QgsVectorLayer(xlsx + "|layername=За границей СЗЗ", "x", "ogr")
        self.assertEqual(near.fields().names()[-1], "Направление от предприятия")

    def test_search_zone_output(self):
        result, _ = run(distance=15, SEARCH="memory:")
        zone = QgsProcessingContextless.layer(result["SEARCH"])
        self.assertEqual(zone.featureCount(), 1)
        # СЗЗ −50..150 плюс 15 м: ≈ 230² с закруглёнными углами
        self.assertAlmostEqual(next(zone.getFeatures()).geometry().area(), 230 * 230, delta=230 * 230 * 0.01)

    def test_szz_is_enterprise_boundary(self):
        result, _ = run(szz=False)
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        near = QgsProcessingContextless.layer(result["NEAR"])
        self.assertEqual(table(inside, "szz_pct"), [(1, "23:47:0107010:7", 30.0)])
        self.assertEqual(table(near, "dist_m"), [
            (1, "23:47:0107010:10", 10.0),
            (2, "23:47:0107010:2", 40.0),
            (3, "23:47:0107010:5", 50.0),
            (4, "23:47:0107010:8", 50.0),
            (5, "23:47:0107010:6", 60.0),
            (6, "23:47:0107010:3", 70.0),
        ])
        self.assertTrue(result["FROM_ENTERPRISE"])
        self.assertEqual(near.fields().names()[-2:], ["dist_m", "dir"])

    def test_word_titles_when_szz_is_boundary(self):
        docx = os.path.join(OUT, "граница.docx")
        run(szz=False, DOCX=docx)
        with zipfile.ZipFile(docx) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("за границей предприятия на расстоянии до 100 м", xml)
        self.assertNotIn("границей границы", xml)
        self.assertIn("Расстояние до границы предприятия, м", xml)

    def test_distance_limits_second_table(self):
        result, _ = run(distance=15)
        near = QgsProcessingContextless.layer(result["NEAR"])
        self.assertEqual([r[1] for r in table(near, "dist_m")],
                         ["23:47:0107010:5", "23:47:0107010:8", "23:47:0107010:6"])

    def test_without_share(self):
        result, _ = run(SHARE=False)
        inside = QgsProcessingContextless.layer(result["INSIDE"])
        self.assertNotIn("szz_pct", inside.fields().names())

    def test_enterprise_from_lines(self):
        lines = layer("LineString", [(ring(0, 0, 100, 100), [])], name="polylines")
        result, _ = run(enterprise=lines)
        self.assertEqual(result["INSIDE_COUNT"], 3)
        self.assertEqual(result["NEAR_COUNT"], 4)
        self.assertEqual(result["OWN_COUNT"], 1)

    def test_unclosed_enterprise_line(self):
        lines = layer("LineString", [(ring(0, 0, 100, 100, closed=False), [])])
        with self.assertRaisesRegex(Exception, "незамкнут.*[Зз]амкните контур"):
            run(enterprise=lines)

    def test_other_crs(self):
        """СЗЗ в Web Mercator, участки в градусах: ответ тот же, расстояния — в метрах UTM."""
        result, _ = run(parcels=parcels_layer("EPSG:4326"), szz=szz_layer("EPSG:3857"))
        near = QgsProcessingContextless.layer(result["NEAR"])
        self.assertEqual(result["INSIDE_COUNT"], 3)
        dist = {f["cad_num"]: f["dist_m"] for f in near.getFeatures()}
        self.assertEqual(set(dist), {"23:47:0107010:5", "23:47:0107010:8", "23:47:0107010:6",
                                     "23:47:0107010:3"})
        # СЗЗ после перевода 32637 → 3857 → 32637 чуть искривлена: допуск 0,5 м
        self.assertAlmostEqual(dist["23:47:0107010:3"], 20.0, delta=0.5)
        self.assertAlmostEqual(dist["23:47:0107010:6"], 10.0, delta=0.5)

    def test_selected_zones_only(self):
        szz = layer("Polygon", [(rect(-50, -50, 150, 150), ["СЗЗ ПС"]),
                                (rect(1000, 1000, 1100, 1100), ["Чужая СЗЗ"])], SZZ_FIELDS)
        szz.selectByIds([1])
        QgsProject.instance().addMapLayer(szz)
        try:
            result, _ = run(szz=QgsProcessingFeatureSourceDefinition(szz.id(), True))
        finally:
            QgsProject.instance().removeMapLayer(szz.id())
        self.assertEqual(result["INSIDE_COUNT"], 3)
        self.assertEqual(result["NEAR_COUNT"], 4)

    def test_zone_list_by_filter(self):
        szz = layer("Polygon", [(rect(1000, 1000, 1100, 1100), ["Чужая СЗЗ"]),
                                (rect(-50, -50, 150, 150), ["СЗЗ ПС"])], SZZ_FIELDS)
        QgsProject.instance().addMapLayer(szz)
        try:
            source = QgsProcessingFeatureSourceDefinition(szz.id(), False)
            source.filterExpression = "$id IN (2)"
            result, _ = run(szz=source)
        finally:
            QgsProject.instance().removeMapLayer(szz.id())
        self.assertEqual(result["INSIDE_COUNT"], 3)

    def test_distance_zero(self):
        result, _ = run(distance=0)
        self.assertEqual(result["NEAR_COUNT"], 2)  # только касающиеся

    def test_files(self):
        xlsx = os.path.join(OUT, "реестр.xlsx")
        docx = os.path.join(OUT, "реестр.docx")
        result, _ = run(XLSX=xlsx, DOCX=docx, OBJECT="ПС 220 кВ Тестовая")
        self.assertEqual(result["XLSX"], xlsx)
        self.assertEqual(result["DOCX"], docx)

        # Excel читается OGR: два листа, заголовки и числа
        book = QgsVectorLayer(xlsx, "x", "ogr")
        sheets = book.dataProvider().subLayers()
        self.assertEqual(len(sheets), 2)
        first = QgsVectorLayer(xlsx + "|layername=В границах СЗЗ", "x", "ogr")
        self.assertTrue(first.isValid())
        self.assertEqual(first.fields().names(), [
            "№ п/п", "Кадастровый номер", "Категория земель", "ВРИ по документу",
            "Доля участка в СЗЗ, %", "Направление от предприятия"])
        rows = [f.attributes() for f in first.getFeatures()]
        self.assertEqual(rows[0][1], "23:47:0107010:2")
        self.assertAlmostEqual(rows[0][4], 50.0)
        second = QgsVectorLayer(xlsx + "|layername=За границей СЗЗ", "x", "ogr")
        self.assertEqual(second.featureCount(), 4)
        self.assertEqual(second.fields().names()[-2:], ["Расстояние до СЗЗ, м", "Направление от предприятия"])

        # Word: A4, поля, две таблицы, заголовок строки повторяется на каждой странице
        with zipfile.ZipFile(docx) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ElementTree.fromstring(xml)
        size = root.find(".//w:sectPr/w:pgSz", ns)
        self.assertEqual((size.get("{%s}w" % ns["w"]), size.get("{%s}h" % ns["w"])),
                         ("11906", "16838"))
        tables = root.findall(".//w:tbl", ns)
        self.assertEqual([len(t.findall("w:tr", ns)) for t in tables], [4, 5])
        self.assertIn("ПС 220 кВ Тестовая", xml)
        self.assertIn("СЗЗ ПС", xml)
        self.assertIn("50,0", xml)
        self.assertEqual(xml.count("<w:tblHeader/>"), 2)
        self.assertIn("Заход меньше 1 м²", xml)
        self.assertIn("населённых пунктов — 0,1 м", xml)
        self.assertIn("промышленности — 0,5 м", xml)

    def test_empty_tables_in_word(self):
        docx = os.path.join(OUT, "пусто.docx")
        far = layer("Polygon", [(rect(5000, 0, 5010, 10), ["1:1:1:1", None, None])], PARCEL_FIELDS)
        result, _ = run(parcels=far, DOCX=docx, XLSX=os.path.join(OUT, "пусто.xlsx"))
        self.assertEqual((result["INSIDE_COUNT"], result["NEAR_COUNT"]), (0, 0))
        with zipfile.ZipFile(docx) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Участков в границах СЗЗ нет.", xml)
        self.assertNotIn("<w:tbl>", xml)

    def test_missing_fields(self):
        bare = layer("Polygon", [(rect(110, 0, 140, 30), ["x"])], "&field=kn_other:string")
        with self.assertRaisesRegex(Exception, "кадастрового номера"):
            run(parcels=bare)
        result, feedback = run(parcels=bare, CAD_FIELD="kn_other")
        self.assertEqual(result["INSIDE_COUNT"], 1)
        self.assertEqual(len([w for w in feedback.warnings if "В слое участков нет поля" in w]), 2)


# ------------------------------------------------------------ загрузка с НСПД (сервер подменён)

class FakeNspd:
    """Подставной НСПД: участки PARCELS, пересекающие присланную область, в EPSG:3857 —
    в формате настоящего ответа intersects (properties.interactionId, properties.options)."""

    def __init__(self, statuses=(), too_big_area_m2=None):
        self.statuses = list(statuses)  # коды, которые вернуть до нормального ответа
        self.too_big = too_big_area_m2   # область больше — ответ 500
        self.calls = []
        to_web = QgsCoordinateTransform(QgsCoordinateReferenceSystem(CRS),
                                        QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
        self.to_wgs = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:3857"),
                                             QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        # как настоящий НСПД: один объект на участок, многоконтурный — MultiPolygon
        grouped = {}
        for cad, box, cat, vri in PARCELS:
            entry = grouped.setdefault(cad, [[], cat, vri])
            if box not in entry[0]:
                entry[0].append(box)
        self.features = []
        for n, (cad, (boxes, cat, vri)) in enumerate(grouped.items(), 1):
            polygons = []
            for box in boxes:
                g = rect(*box)
                g.transform(to_web)
                polygons.append([[[p.x(), p.y()] for p in g.asPolygon()[0]]])
            self.features.append({
                "type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": polygons},
                "properties": {"interactionId": 1000 + n, "options": {
                    "cad_num": cad, "land_record_category_type": cat,
                    "permitted_use_established_by_document": vri,
                    "readable_address": ["край Краснодарский", "г. Новороссийск"]}}})

    def __call__(self, body, feedback=None):
        from osgeo import ogr
        area = QgsGeometry.fromWkt(ogr.CreateGeometryFromJson(
            json.dumps(body["geom"]["features"][0]["geometry"])).ExportToWkt())
        self.calls.append(area)
        assert body["categories"] == [{"id": 36368}]
        if self.statuses:
            return self.statuses.pop(0), b""
        if self.too_big is not None:
            metric = QgsGeometry(area)
            metric.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                                    QgsCoordinateReferenceSystem(CRS), QgsProject.instance()))
            if metric.area() > self.too_big:
                return 500, b""
        found = []
        for f in self.features:
            g = nspd.geometry(f["geometry"])
            g.transform(self.to_wgs)
            if g.intersects(area):
                found.append(f)
        return 200, json.dumps({"type": "FeatureCollection", "features": found}).encode()


def use_nspd(fake):
    nspd.TRANSPORT = fake
    nspd.SLEEP = lambda seconds: None
    return fake


def run_nspd(distance=100.0, szz=False, name="нспд.gpkg", **extra):
    path = os.path.join(OUT, name)
    params = {"ENTERPRISE": enterprise_layer(), "DISTANCE": distance, "OUTPUT": path}
    if szz is not False:
        params["SZZ"] = szz or szz_layer()
    params.update(extra)
    feedback = Feedback()
    result = processing.run("szzparcels:nspd", params, feedback=feedback)
    return result, QgsVectorLayer(result["OUTPUT"], "n", "ogr"), feedback


class NspdTest(unittest.TestCase):
    def tearDown(self):
        nspd.TRANSPORT = nspd.post
        nspd.SLEEP = time.sleep

    def test_payload_is_wgs84_geojson(self):
        body = nspd.payload(QgsGeometry.fromWkt("POLYGON((37.7 44.7, 37.8 44.7, 37.8 44.8, 37.7 44.7))"))
        self.assertEqual(body["categories"], [{"id": 36368}])
        geometry = body["geom"]["features"][0]["geometry"]
        self.assertEqual(geometry["type"], "Polygon")
        self.assertAlmostEqual(geometry["coordinates"][0][0][0], 37.7)

    def test_loads_parcels_in_search_zone(self):
        fake = use_nspd(FakeNspd())
        result, loaded, _ = run_nspd(distance=100)
        self.assertTrue(loaded.isValid())
        self.assertEqual(loaded.crs().authid(), CRS)  # в СК границы предприятия, не 3857
        cads = sorted(set(f["cad_num"] for f in loaded.getFeatures()))
        # граница 0..100 + 100 м: всё, кроме :4 (300..320) и :6 (−95..−60 — заходит в −100)
        self.assertNotIn("23:47:0107010:4", cads)
        self.assertIn("23:47:0107010:3", cads)
        self.assertEqual(result["COUNT"], loaded.featureCount())
        # каждый участок — одна запись; многоконтурный :6 — оба контура; адрес-список — строкой
        all_cads = [f["cad_num"] for f in loaded.getFeatures()]
        self.assertEqual(len(all_cads), len(set(all_cads)))
        ten = next(f for f in loaded.getFeatures() if f["cad_num"] == "23:47:0107010:10")
        self.assertEqual(ten["readable_address"], "край Краснодарский; г. Новороссийск")
        self.assertAlmostEqual(ten.geometry().area(), 900, delta=1)  # 30×30 м после 3857 → МСК
        self.assertEqual(len(fake.calls), 1)

    def test_registry_on_loaded_layer(self):
        """Загруженный слой сразу годится для реестра: те же ответы, что на исходных участках."""
        use_nspd(FakeNspd())
        _, loaded, _ = run_nspd(distance=100, szz=None)
        result, _ = run(parcels=loaded)
        self.assertEqual((result["INSIDE_COUNT"], result["NEAR_COUNT"], result["OWN_COUNT"]), (3, 4, 1))

    def test_retries_on_597(self):
        fake = use_nspd(FakeNspd(statuses=[597, 597]))
        result, _, _ = run_nspd()
        self.assertGreater(result["COUNT"], 0)
        self.assertEqual(len(fake.calls), 3)

    def test_too_big_area_split(self):
        fake = use_nspd(FakeNspd(too_big_area_m2=30000))
        result, loaded, _ = run_nspd(distance=100)
        self.assertGreater(len(fake.calls), 4)  # по частям
        cads = [f["cad_num"] for f in loaded.getFeatures()]
        self.assertEqual(len(cads), len(set(cads)))  # участок на стыке частей — один раз
        use_nspd(FakeNspd())
        whole, _, _ = run_nspd(distance=100, name="нспд_целиком.gpkg")
        self.assertEqual(result["COUNT"], whole["COUNT"])

    def test_real_request_over_http(self):
        """nspd.post по-настоящему: заголовки браузера, JSON-тело, код и тело ответа."""
        import http.server
        import threading
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["headers"] = dict(self.headers)
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps({"features": [{"properties": {"interactionId": 1}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old = nspd.URL
        nspd.URL = "http://127.0.0.1:{}/api/geoportal/v1/intersects".format(server.server_port)
        try:
            body = nspd.payload(QgsGeometry.fromWkt("POLYGON((37.7 44.7, 37.8 44.7, 37.8 44.8, 37.7 44.7))"))
            status, data = nspd.post(body)
        finally:
            nspd.URL = old
            server.shutdown()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["features"][0]["properties"]["interactionId"], 1)
        self.assertEqual(seen["body"], body)
        self.assertEqual(seen["headers"]["Origin"], "https://nspd.gov.ru")
        self.assertIn("Chrome/140", seen["headers"]["User-Agent"])  # не «QGIS/…»
        self.assertIn("sec-ch-ua", {k.lower() for k in seen["headers"]})

    def test_closed_port_is_no_connection(self):
        old = nspd.URL
        nspd.URL = "http://127.0.0.1:9/"
        try:
            self.assertEqual(nspd.post({})[0], 0)
        finally:
            nspd.URL = old

    def test_bundled_certificates(self):
        certs = nspd._ca_certificates()
        names = sorted(c.subjectInfo(c.SubjectInfo.CommonName)[0] for c in certs)
        self.assertEqual(names, ["Russian Trusted Root CA", "Russian Trusted Sub CA"])

    def test_no_connection_message(self):
        use_nspd(lambda body, feedback=None: (0, b""))
        with self.assertRaisesRegex(Exception, "доступен только из России"):
            run_nspd()

    def test_blocked_message(self):
        use_nspd(lambda body, feedback=None: (597, b""))
        with self.assertRaisesRegex(Exception, "Подождите минуту"):
            run_nspd()



# ------------------------------------------------------------ модуль в QGIS

class MessageBar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, title, text, level, duration):
        self.messages.append((text, level))

    def createMessage(self, title, text):
        from qgis.gui import QgsMessageBarItem
        return QgsMessageBarItem(title, text)

    def pushWidget(self, widget, level, duration):
        self.messages.append((widget.text(), level))
        self.widget = widget


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
        self.bar = MessageBar()
        self.menu = []
        self.plugin_menu = QMenu("Модули")

    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self.bar
    def addToolBar(self, name):
        # как в настоящем QGIS: панель в окне, но владеет ею Python
        bar = QToolBar(name)
        self.window.addToolBar(bar)
        sip.transferback(bar)
        return bar
    def pluginMenu(self): return self.plugin_menu
    def addPluginToMenu(self, m, a): self.menu.append((m, a.text()))
    def removePluginMenu(self, m, a): self.menu.remove((m, a.text()))


def toolbars(iface):
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return [b for b in iface.window.findChildren(QToolBar) if b.objectName() == "AltanEcoToolbar"]


def bands(iface):
    return [i for i in iface.canvas.scene().items() if isinstance(i, QgsRubberBand)]


class PluginTest(unittest.TestCase):
    def setUp(self):
        QgsProject.instance().clear()
        self.registry = QgsApplication.processingRegistry()
        self.registry.removeProvider(PROVIDER)
        self.iface = Iface()
        self.plugin = szz_parcels.classFactory(self.iface)
        self.plugin.initProcessing()  # так делает QGIS до initGui() при hasProcessingProvider=yes
        self.plugin.initGui()
        self.parcels = parcels_layer()
        self.enterprise = enterprise_layer()
        self.szz = layer("Polygon", [(rect(-50, -50, 150, 150), ["СЗЗ ПС Тестовая"]),
                                     (rect(1000, 1000, 1100, 1100), ["Чужая СЗЗ"])],
                         SZZ_FIELDS, name="СЗЗ из НСПД")
        self.enterprise.setName("Граница предприятия")
        for lyr in (self.parcels, self.enterprise, self.szz):
            QgsProject.instance().addMapLayer(lyr)

    def tearDown(self):
        global PROVIDER
        self.plugin.unload()
        # ссылку держим: иначе Python удалит провайдер вместе с алгоритмами
        PROVIDER = SzzParcelsProvider()
        self.registry.addProvider(PROVIDER)
        QgsProject.instance().clear()

    def open(self):
        self.plugin.run()
        app.processEvents()
        dialog = self.plugin.dialog
        dialog.xlsx_on.setChecked(False)
        dialog.docx_on.setChecked(False)
        dialog.same.setChecked(False)
        dialog.distance.setValue(100)  # окно помнит расстояние с прошлого раза
        dialog.update_preview()
        return dialog

    def wait(self):
        end = time.monotonic() + 20
        while self.plugin._running is not None and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.plugin._running, "расчёт не закончился")

    def test_button_and_menu(self):
        self.assertEqual(len(toolbars(self.iface)), 1)
        self.assertEqual(self.iface.menu, [("&Альтан-Эко", "Реестр участков в СЗЗ")])
        self.assertIsNotNone(self.registry.algorithmById(ALG))

    def test_defaults_from_project(self):
        dialog = self.open()
        self.assertIs(dialog.parcels.currentLayer(), self.parcels)
        self.assertIs(dialog.szz.currentLayer(), self.szz)
        self.assertIs(dialog.enterprise.currentLayer(), self.enterprise)
        self.assertEqual(dialog.cad_field.currentField(), "cad_num")
        self.assertEqual(dialog.category_field.currentField(), "land_record_category_type")
        self.assertEqual(dialog.vri_field.currentField(), "permitted_use_established_by_document")

    def test_asks_which_zones(self):
        dialog = self.open()
        self.assertTrue(dialog._many_zones)
        self.assertEqual(dialog.zone_list.count(), 2)
        self.assertIn("СЗЗ ПС Тестовая", dialog.zone_list.item(0).text())
        # все зоны
        self.assertEqual(dialog.counts, (3, 4, 1))
        # список без отметок — ошибка, кнопка недоступна
        dialog.zone_list_radio.setChecked(True)
        dialog.update_preview()
        self.assertIn("хотя бы одну зону", dialog.error)
        self.assertFalse(dialog.run_button.isEnabled())
        # только чужая зона — участков нет
        dialog.zone_list.item(1).setCheckState(Qt.CheckState.Checked)
        dialog.update_preview()
        self.assertIsNone(dialog.error)
        self.assertEqual(dialog.counts, (0, 0, 0))
        self.assertIn("$id IN (2)", dialog.parameters()["SZZ"].filterExpression)
        # выделение на карте — выбирается само
        self.szz.selectByIds([1])
        dialog._layers_changed()
        dialog.update_preview()
        self.assertEqual(dialog.zone_mode.checkedId(), 1)
        self.assertEqual(dialog.counts, (3, 4, 1))
        self.assertTrue(dialog.parameters()["SZZ"].selectedFeaturesOnly)

    def test_highlight_follows_distance(self):
        dialog = self.open()
        self.assertEqual(dialog.counts, (3, 4, 1))
        # граница поиска, СЗЗ, «в СЗЗ», «рядом»
        self.assertEqual(len(bands(self.iface)), 4)
        dialog.distance.setValue(15)
        dialog.update_preview()
        self.assertEqual(dialog.counts, (3, 3, 1))
        self.assertIn("до 15 м — 3", dialog.status.text())
        dialog.distance.setValue(0)
        dialog.update_preview()
        self.assertEqual(dialog.counts, (3, 2, 1))

    def test_szz_is_enterprise(self):
        dialog = self.open()
        dialog.same.setChecked(True)
        dialog.update_preview()
        self.assertFalse(dialog._many_zones)
        self.assertEqual(dialog.counts, (1, 6, 1))
        self.assertNotIn("SZZ", dialog.parameters())

    def test_dialog_shrinks_when_zones_hidden(self):
        dialog = self.open()
        dialog.show()
        app.processEvents()
        self.assertTrue(dialog.zones_box.isVisible())
        body = dialog.scroll.widget()
        dialog.same.setChecked(True)
        app.processEvents()
        self.assertFalse(dialog.zones_box.isVisible())
        # вопрос о зонах не оставил пустого места: содержимое не растянуто сверх нужного
        self.assertLessEqual(body.height(), body.sizeHint().height() + 5)
        dialog.same.setChecked(False)
        app.processEvents()
        self.assertTrue(dialog.zones_box.isVisible())
        self.assertLessEqual(body.height(), body.sizeHint().height() + 5)


    def test_checkbox_texts_not_clipped(self):
        """Подписи галочек целиком — и с вопросом о зонах, и без (Fusion, шрифт как в Windows)."""
        from qgis.PyQt.QtWidgets import QCheckBox
        dialog = self.open()
        dialog.show()
        for same in (False, True):
            dialog.same.setChecked(same)
            app.processEvents()
            for box in dialog.findChildren(QCheckBox):
                if box.isVisible() and box.text():
                    self.assertGreaterEqual(box.width(), box.sizeHint().width(), box.text())

    def test_dialog_fits_screen(self):
        dialog = self.open()
        dialog.show()
        app.processEvents()
        self.assertTrue(dialog.zones_box.isVisible())
        screen = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.frameGeometry().height(), screen.height())
        # всё содержимое доступно прокруткой
        self.assertTrue(dialog.scroll.widgetResizable())

    def test_error_readable_in_dark_theme(self):
        lines = layer("LineString", [(ring(0, 0, 100, 100, closed=False), [])], name="линия")
        QgsProject.instance().addMapLayer(lines)
        dialog = self.open()
        dialog.enterprise.setLayer(lines)
        dialog.update_preview()
        self.assertNotIn("#b00", dialog.status.text())  # жёсткий тёмно-красный не виден на тёмном фоне
        self.assertIn("⚠", dialog.status.text())

    def test_szz_layer_not_guessed(self):
        """Нет слоя с «СЗЗ» в названии — слой СЗЗ не подставляется (раньше брался слой участков)."""
        QgsProject.instance().removeMapLayer(self.szz.id())
        dialog = self.open()
        self.assertIsNone(dialog.szz.currentLayer())
        self.assertIn("слой СЗЗ", dialog.error)

    def test_file_checkboxes_have_tooltips(self):
        dialog = self.open()
        self.assertIn("Excel", dialog.xlsx_on.toolTip())
        self.assertIn("Word", dialog.docx_on.toolTip())

    def test_default_files_not_overwritten(self):
        """Прежний реестр с тем же именем не затирается: новый — « 2», без скобок в имени."""
        project_dir = os.path.join(OUT, "Проект для повтора")
        os.makedirs(project_dir, exist_ok=True)
        QgsProject.instance().setFileName(os.path.join(project_dir, "п.qgz"))
        dialog = self.open()
        dialog.xlsx_on.setChecked(True)
        dialog.docx_on.setChecked(False)
        old = dialog.xlsx.filePath()
        os.makedirs(os.path.dirname(old), exist_ok=True)
        with open(old, "w") as f:
            f.write("прежний реестр")
        dialog._run()
        self.wait()
        with open(old) as f:
            self.assertEqual(f.read(), "прежний реестр")
        new = old.replace(".xlsx", " 2.xlsx")
        self.assertTrue(os.path.isfile(new))
        self.assertIn("Реестр участков в СЗЗ 2.xlsx", self.iface.bar.messages[-1][0])

    def test_unwritable_file_message(self):
        dialog = self.open()
        busy = os.path.join(OUT, "занят2.xlsx")
        os.makedirs(busy, exist_ok=True)
        dialog.xlsx_on.setChecked(True)
        dialog.xlsx.setFilePath(busy)
        dialog._run()
        self.wait()
        group = QgsProject.instance().layerTreeRoot().findGroup("Реестр участков в СЗЗ")
        self.assertIsNotNone(group)  # слои не потерялись
        texts = [t for t, level in self.iface.bar.messages if level == Qgis.MessageLevel.Warning]
        self.assertTrue(any("Не удалось сохранить" in t and "закройте" in t for t in texts), texts)


    def test_nspd_button_loads_and_selects_layer(self):
        use_nspd(FakeNspd())
        project_dir = os.path.join(OUT, "Проект НСПД")
        os.makedirs(project_dir, exist_ok=True)
        QgsProject.instance().setFileName(os.path.join(project_dir, "п.qgz"))
        dialog = self.open()
        dialog.same.setChecked(True)
        self.assertTrue(dialog.nspd_button.isEnabled())
        expected = os.path.join(project_dir, "Карты СЗЗ", "Участки НСПД {}.gpkg".format(
            __import__("datetime").date.today().isoformat()))
        self.assertEqual(dialog.nspd_parameters()["OUTPUT"], expected)
        try:
            dialog.nspd_button.click()
            self.wait()
        finally:
            nspd.TRANSPORT = nspd.post
            nspd.SLEEP = time.sleep
        self.assertTrue(os.path.isfile(expected))
        layer = dialog.parcels.currentLayer()
        self.assertEqual(layer.source().split("|")[0], expected)
        self.assertEqual(dialog.cad_field.currentField(), "cad_num")
        text, level = self.iface.bar.messages[-1]
        self.assertEqual(level, Qgis.MessageLevel.Success)
        self.assertIn("Загружено участков с НСПД", text)
        # второй раз в тот же день — новый файл « 2», прежний не тронут
        self.assertTrue(dialog.nspd_parameters()["OUTPUT"].endswith(" 2.gpkg"))

    def test_nspd_real_network_in_background(self):
        """Кнопка → фоновая задача → настоящий запрос по HTTP к подставному НСПД."""
        import http.server
        import threading
        fake = FakeNspd()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, data = fake(body)
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = nspd.URL
        nspd.URL = "http://127.0.0.1:{}/".format(server.server_port)
        dialog = self.open()
        dialog.same.setChecked(True)
        try:
            dialog.nspd_button.click()
            self.wait()
        finally:
            nspd.URL = old
            server.shutdown()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.iface.bar.messages[-1][1], Qgis.MessageLevel.Success)
        self.assertGreater(dialog.parcels.currentLayer().featureCount(), 0)

    def test_nspd_error_shown(self):
        use_nspd(lambda body, feedback=None: (0, b""))
        dialog = self.open()
        dialog.same.setChecked(True)
        try:
            dialog.nspd_button.click()
            self.wait()
        finally:
            nspd.TRANSPORT = nspd.post
        text, level = self.iface.bar.messages[-1]
        self.assertEqual(level, Qgis.MessageLevel.Critical)
        self.assertIn("доступен только из России", text)
        self.assertTrue(dialog.nspd_button.isEnabled())  # можно повторить

    def test_nspd_button_does_not_squeeze_layer_list(self):
        dialog = self.open()
        dialog.show()
        app.processEvents()
        self.assertGreaterEqual(dialog.parcels.width(), dialog.cad_field.width() - 2)

    def test_no_parcels_suggests_nspd(self):
        QgsProject.instance().removeMapLayer(self.parcels.id())
        dialog = self.open()
        self.assertIn("Загрузить с НСПД", dialog.error)

    def test_tolerances_in_advanced_box(self):
        dialog = self.open()
        self.assertTrue(dialog.advanced.isCollapsed())  # по умолчанию свёрнуто
        self.assertEqual(dialog.depth_spins["industry"].value(), 0.5)
        dialog.depth_spins["industry"].setValue(0.3)
        dialog.min_area.setValue(2)
        params = dialog.parameters()
        self.assertEqual((params["DEPTH_INDUSTRY"], params["MIN_OVERLAP"]), (0.3, 2))
        dialog.close()
        self.plugin.unload()
        self.plugin.initGui()
        self.plugin.run()
        self.assertEqual(self.plugin.dialog.depth_spins["industry"].value(), 0.3)  # запомнено
        self.plugin.dialog.defaults_button.click()
        self.assertEqual(self.plugin.dialog.depth_spins["industry"].value(), 0.5)
        self.assertEqual(self.plugin.dialog.min_area.value(), 1)

    def test_result_layers_not_picked_as_boundary(self):
        """Слои прошлого реестра («В границах СЗЗ», «За границей предприятия…») — не граница и не СЗЗ."""
        QgsProject.instance().removeMapLayer(self.enterprise.id())
        lines = layer("LineString", [(ring(0, 0, 100, 100), [])], name="участок — features")
        QgsProject.instance().addMapLayer(lines)
        dialog = self.open()
        dialog.enterprise.setLayer(lines)
        dialog.same.setChecked(True)
        dialog._run()
        self.wait()
        dialog.close()
        names = [l.name() for l in QgsProject.instance().mapLayers().values()]
        self.assertIn("За границей предприятия до 100 м", names)
        self.plugin.unload()
        self.plugin.initGui()
        self.plugin.run()
        again = self.plugin.dialog
        self.assertIs(again.enterprise.currentLayer(), lines)  # выбор прошлого расчёта
        self.assertIs(again.parcels.currentLayer(), self.parcels)

    def test_result_layers_never_default(self):
        """Без запомненного выбора эвристика не берёт слои результата модуля."""
        QgsProject.instance().removeMapLayer(self.enterprise.id())
        fake_result = layer("Polygon", [(rect(0, 0, 10, 10), [])], name="За границей предприятия до 500 м")
        fake_result.setCustomProperty("szz_parcels/result", True)
        old_name = layer("Polygon", [(rect(0, 0, 10, 10), [])], name="В границах СЗЗ")
        for lyr in (fake_result, old_name):
            QgsProject.instance().addMapLayer(lyr)
        dialog = self.open()
        self.assertNotIn(dialog.enterprise.currentLayer(), (fake_result, old_name))
        self.assertNotIn(dialog.szz.currentLayer(), (fake_result, old_name))

    def test_map_pdf_and_layout(self):
        from qgis.core import QgsLayoutItemMap
        project_dir = os.path.join(OUT, "Проект с картой")
        os.makedirs(project_dir, exist_ok=True)
        QgsProject.instance().setFileName(os.path.join(project_dir, "п.qgz"))
        dialog = self.open()
        dialog.map_on.setChecked(True)
        dialog.object_name.setText("ПС Тестовая")
        for _ in range(2):  # второй раз — тот же макет заменяется, PDF — с номером
            dialog._run()
            self.wait()
        manager = QgsProject.instance().layoutManager()
        layouts = [l.name() for l in manager.layouts()]
        self.assertEqual(layouts.count("Карта СЗЗ"), 1)
        layout = manager.layoutByName("Карта СЗЗ")
        maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
        self.assertEqual(len(maps), 1)
        zone = [l for l in QgsProject.instance().mapLayers().values() if l.name().startswith("Зона поиска")]
        self.assertTrue(zone)
        self.assertTrue(maps[0].extent().contains(zone[-1].extent()))
        folder = os.path.join(project_dir, "Карты СЗЗ")
        pdfs = sorted(f for f in os.listdir(folder) if f.endswith(".pdf"))
        self.assertEqual(pdfs, ["Карта СЗЗ 2.pdf", "Карта СЗЗ.pdf"])
        self.assertGreater(os.path.getsize(os.path.join(folder, "Карта СЗЗ.pdf")), 5000)
        near = [l for l in QgsProject.instance().mapLayers().values() if l.name().startswith("За границей")][-1]
        self.assertTrue(near.labelsEnabled())  # подписи — конец кадастрового номера
        # прошлый расчёт в группе выключен, новый — включён
        group = QgsProject.instance().layerTreeRoot().findGroup("Реестр участков в СЗЗ")
        visible = [(n.layer().name(), n.isVisible()) for n in group.findLayers()]
        self.assertEqual(sum(1 for _, v in visible if v), 3, visible)
        # масштаб — круглый
        self.assertIn(round(maps[0].scale()), (500, 1000, 2000, 2500, 5000, 10000, 25000))
        # в легенде — «Граница предприятия», а не имя слоя
        from qgis.core import QgsLayoutItemLegend
        legend = [i for i in layout.items() if isinstance(i, QgsLayoutItemLegend)][0]
        # подпись, которую рисует легенда: у слоя с одним символом — подпись символа
        titles = []
        for n in legend.model().rootGroup().children():
            nodes = legend.model().layerLegendNodes(n)
            titles.append(nodes[0].data(Qt.ItemDataRole.DisplayRole) if len(nodes) == 1 else
                          n.customProperty("legend/title-label") or n.name())
        self.assertIn("Граница предприятия", titles)
        self.assertIn("Санитарно-защитная зона", titles)  # слой «СЗЗ из НСПД» подписан по смыслу
        # зона поиска — над участками, иначе её пунктир под заливкой
        self.assertTrue(group.children()[0].layer().name().startswith("Зона поиска"))
        self.assertIn("Карта СЗЗ.pdf", " ".join(t for t, _ in self.iface.bar.messages))

    def test_direction_controls(self):
        dialog = self.open()
        dialog.direction.setChecked(True)
        dialog.direction_from.setCurrentIndex(1)
        params = dialog.parameters()
        self.assertEqual((params["DIRECTION"], params["DIRECTION_FROM"]), (True, 1))
        dialog.direction.setChecked(False)
        self.assertFalse(dialog.direction_from.isEnabled())
        self.assertFalse(dialog.parameters()["DIRECTION"])
        dialog.close()
        self.plugin.unload()
        self.plugin.initGui()
        self.plugin.run()
        self.assertFalse(self.plugin.dialog.direction.isChecked())  # запомнено
        self.assertEqual(self.plugin.dialog.direction_from.currentIndex(), 1)
        self.plugin.dialog.direction.setChecked(True)

    def test_map_off(self):
        dialog = self.open()
        dialog.map_on.setChecked(False)
        dialog._run()
        self.wait()
        self.assertIsNone(QgsProject.instance().layoutManager().layoutByName("Карта СЗЗ"))

    def test_nspd_button_needs_boundary(self):
        QgsProject.instance().removeMapLayer(self.szz.id())
        dialog = self.open()
        dialog.same.setChecked(False)
        self.assertFalse(dialog.nspd_button.isEnabled())  # СЗЗ не выбрана
        dialog.same.setChecked(True)
        self.assertTrue(dialog.nspd_button.isEnabled())

    def test_unclosed_line_reported(self):
        lines = layer("LineString", [(ring(0, 0, 100, 100, closed=False), [])], name="линия")
        QgsProject.instance().addMapLayer(lines)
        dialog = self.open()
        dialog.enterprise.setLayer(lines)
        dialog.update_preview()
        self.assertIn("не замкнута", dialog.error)
        self.assertIn("соедините концы", dialog.error)  # ошибка говорит, что делать
        self.assertFalse(dialog.run_button.isEnabled())

    def test_run_makes_layers_and_files(self):
        dialog = self.open()
        dialog.szz.setLayer(self.szz)
        self.szz.selectByIds([1])
        dialog.object_name.setText("ПС Тестовая")
        dialog.xlsx_on.setChecked(True)
        dialog.docx_on.setChecked(True)
        dialog.xlsx.setFilePath(os.path.join(OUT, "окно.xlsx"))
        dialog.docx.setFilePath(os.path.join(OUT, "окно.docx"))
        dialog._run()
        self.wait()
        group = QgsProject.instance().layerTreeRoot().findGroup("Реестр участков в СЗЗ")
        self.assertIsNotNone(group)
        names = [node.layer().name() for node in group.findLayers()]
        self.assertEqual(names, ["Зона поиска до 100 м", "В границах СЗЗ", "За границей СЗЗ до 100 м"])
        by_name = {node.layer().name(): node.layer() for node in group.findLayers()}
        inside = by_name["В границах СЗЗ"]
        self.assertEqual(inside.featureCount(), 3)
        self.assertEqual(inside.attributeDisplayName(inside.fields().indexOf("szz_pct")),
                         "Доля участка в СЗЗ, %")
        near = by_name["За границей СЗЗ до 100 м"]
        self.assertEqual(near.attributeDisplayName(near.fields().indexOf("dist_m")),
                         "Расстояние до СЗЗ, м")
        text, level = self.iface.bar.messages[-1]
        self.assertEqual(level, Qgis.MessageLevel.Success)
        self.assertIn("В границах СЗЗ: 3", text)
        self.assertTrue(os.path.exists(os.path.join(OUT, "окно.xlsx")))
        self.assertTrue(os.path.exists(os.path.join(OUT, "окно.docx")))
        self.assertEqual(self.parcels.featureCount(), len(PARCELS) + 1)  # слой участков не менялся

    def test_error_shown(self):
        dialog = self.open()
        params = dialog.parameters()
        params["CAD_FIELD"] = "нет_такого"
        self.plugin.start(params)
        self.wait()
        text, level = self.iface.bar.messages[-1]
        self.assertEqual(level, Qgis.MessageLevel.Critical)
        self.assertIn("нет поля", text)

    def test_files_go_to_szz_folder_next_to_project(self):
        project_dir = os.path.join(OUT, "Проект ПС")
        os.makedirs(project_dir, exist_ok=True)
        QgsProject.instance().setFileName(os.path.join(project_dir, "Проект.qgz"))
        folder = os.path.join(project_dir, "Карты СЗЗ")
        self.assertFalse(os.path.exists(folder))
        dialog = self.open()
        self.assertEqual(dialog.xlsx.filePath(),
                         os.path.join(folder, "Реестр участков в СЗЗ.xlsx"))
        self.assertEqual(dialog.docx.filePath(),
                         os.path.join(folder, "Реестр участков в СЗЗ.docx"))
        self.assertFalse(os.path.exists(folder))  # папка — только при расчёте
        dialog.xlsx_on.setChecked(True)
        dialog.docx_on.setChecked(True)
        dialog._run()
        self.wait()
        self.assertEqual(sorted(os.listdir(folder)),
                         ["Карта СЗЗ.pdf", "Реестр участков в СЗЗ.docx", "Реестр участков в СЗЗ.xlsx"])
        # путь, выбранный вручную, не подменяется при повторном открытии
        own = os.path.join(OUT, "свой.xlsx")
        dialog.xlsx.setFilePath(own)
        dialog.close()
        self.plugin.run()
        self.assertEqual(self.plugin.dialog.xlsx.filePath(), own)
        # а путь по умолчанию следует за проектом
        other = os.path.join(OUT, "Другой проект")
        os.makedirs(other, exist_ok=True)
        dialog.close()
        QgsProject.instance().setFileName(os.path.join(other, "п.qgz"))
        self.plugin.run()
        self.assertEqual(self.plugin.dialog.docx.filePath(),
                         os.path.join(other, "Карты СЗЗ", "Реестр участков в СЗЗ.docx"))

    def test_remembers_distance(self):
        dialog = self.open()
        dialog.distance.setValue(250)
        dialog.close()
        self.plugin.unload()
        self.plugin.initGui()
        self.plugin.run()
        self.assertEqual(self.plugin.dialog.distance.value(), 250)

    def test_close_and_unload_leave_nothing(self):
        dialog = self.open()
        self.assertTrue(bands(self.iface))
        dialog.close()
        self.assertEqual(bands(self.iface), [])
        # сигналы слоёв отключены: выделение не перезапускает подсветку
        self.szz.selectByIds([1])
        self.assertFalse(dialog._timer.isActive())
        # Esc (reject) — тоже без следов
        dialog = self.open()
        dialog.show()
        self.assertTrue(bands(self.iface))
        dialog.reject()
        self.assertEqual(bands(self.iface), [])
        self.open()
        self.plugin.unload()
        self.assertEqual(bands(self.iface), [])
        self.assertIsNone(self.plugin.dialog)
        self.assertEqual(toolbars(self.iface), [])
        self.assertEqual(self.iface.menu, [])
        self.assertIsNone(self.registry.algorithmById(ALG))
        self.plugin.initGui()  # для tearDown


class QgsProcessingContextless:
    """Слой результата processing.run: объект слоя или id в проекте."""

    @staticmethod
    def layer(value):
        if isinstance(value, QgsVectorLayer):
            return value
        found = QgsProject.instance().mapLayer(value)
        if found is None:
            from processing.tools import dataobjects  # noqa: F401
            from qgis.core import QgsProcessingUtils, QgsProcessingContext
            found = QgsProcessingUtils.mapLayerFromString(value, QgsProcessingContext())
        return found


def _leftover_dirs():
    """Папки тестовой организации вне tests/_profile: их создают Qt и initQgis()."""
    from qgis.PyQt.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    found = [os.path.join(base, ORG)]
    path = QgsApplication.qgisSettingsDirPath().rstrip("/")
    while path and os.path.dirname(path) != path:
        if os.path.basename(path) == ORG:
            found.append(path)
        path = os.path.dirname(path)
    return [p for p in found if os.path.basename(p) == ORG and not p.startswith(PROFILE)]


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    leftovers = _leftover_dirs()
    QgsProject.instance().clear()
    app.exitQgis()
    shutil.rmtree(PROFILE, ignore_errors=True)
    for path in leftovers:
        shutil.rmtree(path, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
