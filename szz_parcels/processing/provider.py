import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .registry_algorithm import SzzRegistryAlgorithm

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))


class SzzParcelsProvider(QgsProcessingProvider):
    def id(self):
        return "szzparcels"

    def name(self):
        return "Участки в СЗЗ"

    def icon(self):
        return QIcon(os.path.join(PLUGIN_DIR, "icon.svg"))

    def loadAlgorithms(self):
        self.addAlgorithm(SzzRegistryAlgorithm())
