"""Окна модуля для снимков: qgis-ui-review/scripts/ui_snap.py (светлая/тёмная тема, крупный шрифт, минимум).

Состояния — как их видит сотрудник: пустой проект, СЗЗ слоем с вопросом о зонах, «СЗЗ совпадает с границей»,
ошибка (незамкнутая граница из чертежа).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

CRS = "EPSG:32637"
X0, Y0 = 400000.0, 5000000.0


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
        self.canvas.setExtent(QgsRectangle(X0 - 200, Y0 - 200, X0 + 400, Y0 + 300))

    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def messageBar(self): return None


def _rect(x1, y1, x2, y2):
    return QgsGeometry.fromRect(QgsRectangle(X0 + x1, Y0 + y1, X0 + x2, Y0 + y2))


def _layer(kind, name, fields, rows):
    layer = QgsVectorLayer("{}?crs={}{}".format(kind, CRS, fields), name, "memory")
    features = []
    for geometry, values in rows:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        feature.setAttributes(values)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def _project(enterprise_closed=True):
    project = QgsProject.instance()
    project.clear()
    parcels = _layer(
        "Polygon", "Земельные участки из ЕГРН",
        "&field=cad_num:string&field=land_record_category_type:string"
        "&field=permitted_use_established_by_document:string",
        [(_rect(10, 10, 90, 90), ["23:47:0107010:9", "Земли промышленности", "Под ПС"]),
         (_rect(110, 0, 140, 30), ["23:47:0107010:10", "Земли населенных пунктов", "ИЖС"]),
         (_rect(170, 0, 190, 20), ["23:47:0107010:3", None, "ЛПХ"])])
    points = [QgsPointXY(X0 + x, Y0 + y) for x, y in ((0, 0), (100, 0), (100, 100), (0, 100))]
    if enterprise_closed:
        points.append(points[0])
    boundary = _layer("LineString", "Граница предприятия", "",
                      [(QgsGeometry.fromPolylineXY(points), [])])
    szz = _layer("Polygon", "СЗЗ из НСПД", "&field=name_by_doc:string",
                 [(_rect(-50, -50, 150, 150), ["Санитарно-защитная зона ПС 110 кВ Южная"]),
                  (_rect(1000, 1000, 1100, 1100), ["Санитарно-защитная зона котельной"])])
    for layer in (parcels, boundary, szz):
        project.addMapLayer(layer)


def windows():
    from szz_parcels.dialog import SzzDialog
    iface = Iface()

    def make(same=False, closed=True, empty=False):
        if empty:
            QgsProject.instance().clear()
        else:
            _project(closed)
        dialog = SzzDialog(iface, lambda params, d=None: None)
        dialog.xlsx_on.setChecked(True)
        dialog.docx_on.setChecked(True)
        dialog.same.setChecked(same)
        dialog.distance.setValue(100)
        dialog.update_preview()
        return dialog

    return [
        ("szz_пустой_проект", lambda: make(empty=True)),
        ("szz_слой_зон", lambda: make()),
        ("szz_по_границе", lambda: make(same=True)),
        ("szz_ошибка_линия", lambda: make(same=True, closed=False)),
    ]
