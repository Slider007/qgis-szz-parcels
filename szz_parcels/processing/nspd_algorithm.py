"""Алгоритм «Загрузить участки с НСПД»: участки в зоне поиска вокруг предприятия или СЗЗ."""

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessingException,
    QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
)
from qgis.PyQt.QtCore import QMetaType

from .. import core, nspd
from .registry_algorithm import SzzRegistryAlgorithm

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")


class NspdParcelsAlgorithm(SzzRegistryAlgorithm):
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return NspdParcelsAlgorithm()

    def name(self):
        return "nspd"

    def displayName(self):
        return self.tr("Загрузить участки с НСПД")

    def shortHelpString(self):
        return self.tr(
            "Земельные участки из ЕГРН с публичной кадастровой карты НСПД (Росреестр), "
            "попадающие в зону поиска: СЗЗ (или граница предприятия) плюс расстояние поиска. "
            "Результат — слой в системе координат границы предприятия с карточкой участка "
            "(кадастровый номер, категория земель, ВРИ, адрес, площадь и др.).\n\n"
            "НСПД доступен только из России. Сведения публичной кадастровой карты справочные и "
            "не заменяют выписку из ЕГРН; участков без координат границ на карте нет.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ENTERPRISE, self.tr("Граница предприятия (полигоны или замкнутые линии)"),
            [Qgis.ProcessingSourceType.VectorPolygon, Qgis.ProcessingSourceType.VectorLine]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SZZ, self.tr("СЗЗ (пусто — СЗЗ по границе предприятия)"),
            [Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterDistance(
            self.DISTANCE, self.tr("Загрузить участки за границей СЗЗ на расстоянии до, м"),
            100.0, self.ENTERPRISE, False, 0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("Участки НСПД"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):
        context.setInvalidGeometryCheck(Qgis.InvalidGeometryCheck.NoCheck)
        enterprise_src = self.parameterAsSource(parameters, self.ENTERPRISE, context)
        if enterprise_src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.ENTERPRISE))
        szz_src = self.parameterAsSource(parameters, self.SZZ, context)
        distance = self.parameterAsDouble(parameters, self.DISTANCE, context)

        # зона поиска — как в реестре: в метрах, затем в EPSG:4326 для НСПД
        out_crs = enterprise_src.sourceCrs()
        rough = [g for _, g in self._geometries(enterprise_src, WGS84, context, feedback)]
        work_crs = core.working_crs(out_crs, QgsGeometry.unaryUnion(rough) if rough else None)
        zone = self._area(self._geometries(szz_src or enterprise_src, work_crs, context, feedback),
                          self.tr("СЗЗ") if szz_src else self.tr("Граница предприятия"))
        area = zone.buffer(distance, 16) if distance > 0 else zone
        area.transform(QgsCoordinateTransform(work_crs, WGS84, context.transformContext()))

        feedback.pushInfo(self.tr("Запрос к НСПД: участки в {:g} м от {}").format(
            distance, self.tr("СЗЗ") if szz_src else self.tr("границы предприятия")))
        try:
            found = nspd.parcels(nspd.fetch(area, feedback))
        except nspd.NspdError as e:
            raise QgsProcessingException(str(e))
        if feedback.isCanceled():
            return {}
        feedback.setProgress(80)

        keys = []
        for _, _, options in found:
            keys.extend(k for k in options if k not in keys)
        fields = QgsFields()
        fields.append(QgsField("interactionId", QMetaType.Type.LongLong))
        for key in keys:
            fields.append(QgsField(key, QMetaType.Type.QString))
        if out_crs.isGeographic() or not out_crs.isValid():
            out_crs = work_crs
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                             Qgis.WkbType.MultiPolygon, out_crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        to_out = QgsCoordinateTransform(WEB_MERCATOR, out_crs, context.transformContext())
        for interaction_id, shape, options in found:
            shape = QgsGeometry(shape)
            shape.transform(to_out)
            shape.convertToMultiType()
            feature = QgsFeature(fields)
            feature.setGeometry(shape)
            try:
                interaction_id = int(interaction_id) if interaction_id is not None else None
            except (TypeError, ValueError):
                interaction_id = None
            feature.setAttributes([interaction_id] + [options.get(k) for k in keys])
            sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
        if hasattr(sink, "finalize"):
            sink.finalize()
        feedback.pushInfo(self.tr("Загружено участков: {}").format(len(found)))
        feedback.setProgress(100)
        return {self.OUTPUT: dest_id, "COUNT": len(found)}
