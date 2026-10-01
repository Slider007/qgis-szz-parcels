import os

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
)
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QPushButton

from . import altan_toolbar

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

PLUGIN_DIR = os.path.dirname(__file__)
ALGORITHM = "szzparcels:registry"
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

            self.dialog = SzzDialog(self.iface, self.start)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def start(self, params, dialog=None):
        if self._running is not None:
            self.message("Реестр ещё считается — ход виден внизу окна QGIS.")
            return
        algorithm = QgsApplication.processingRegistry().createAlgorithmById(ALGORITHM)
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = _Feedback()
        task = QgsProcessingAlgRunnerTask(algorithm, params, context, feedback)
        task.setDescription(TITLE)
        self._running = (task, context, feedback, params)
        task.executed.connect(self.finished)
        if dialog is not None:
            dialog.run_button.setEnabled(False)
        QgsApplication.taskManager().addTask(task)

    def finished(self, ok, results):
        running, self._running = self._running, None
        if self.dialog is not None:
            self.dialog.run_button.setEnabled(self.dialog.error is None)
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
        layers = []
        for key in ("INSIDE", "NEAR"):
            layer = context.takeResultLayer(results[key])
            if layer is None:
                continue
            layer.setName(names[key])
            apply_aliases(layer, INSIDE_ALIASES if key == "INSIDE" else near_aliases(from_enterprise))
            symbol = layer.renderer().symbol() if layer.renderer() else None
            if symbol is not None:  # те же цвета, что у подсветки в окне
                symbol.setColor(QColor(214, 39, 40, 110) if key == "INSIDE"
                                else QColor(255, 140, 0, 110))
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            layers.append(layer)

        text = "В границах СЗЗ: {}, за границей до {:g} м: {}.".format(
            results.get("INSIDE_COUNT", 0), distance, results.get("NEAR_COUNT", 0))
        files = [results[k] for k in ("XLSX", "DOCX") if results.get(k)]
        if files:
            text += " Файлы: {} — в папке «{}».".format(
                ", ".join("«{}»".format(os.path.basename(f)) for f in files),
                os.path.basename(os.path.dirname(files[0])))
        widget = self.iface.messageBar().createMessage(TITLE, text)
        for path in files:
            button = QPushButton("Открыть {}".format("Excel" if path.endswith(".xlsx") else "Word"))
            button.clicked.connect(lambda _=False, p=path: QDesktopServices.openUrl(
                QUrl.fromLocalFile(p)))
            widget.layout().addWidget(button)
        self.iface.messageBar().pushWidget(widget, Qgis.MessageLevel.Success, 0)
        if feedback.warnings:
            self.message(" ".join(feedback.warnings), Qgis.MessageLevel.Warning, 0)
        return layers
