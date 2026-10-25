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
    QgsProcessingParameterString,
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
            self.SZZ_WIDTH, self.tr("Без слоя СЗЗ: размер СЗЗ от границы предприятия, м "
                                    "(0 — СЗЗ по границе)"), 0.0, self.ENTERPRISE, False, 0.0))
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
        if szz_src is None:  # СЗЗ размером: граница + N м, зона поиска — ещё на расстояние дальше
            distance += self.parameterAsDouble(parameters, self.SZZ_WIDTH, context)

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
        if out_crs.isGeographic() or not out_crs.isValid():
            out_crs = work_crs
        dest_id = self._write(found, parameters, context, out_crs)
        feedback.pushInfo(self.tr("Загружено участков: {}").format(len(found)))
        feedback.setProgress(100)
        return {self.OUTPUT: dest_id, "COUNT": len(found)}

    def _write(self, found, parameters, context, out_crs):
        """Участки из ответа НСПД (EPSG:3857) — в слой OUTPUT в out_crs с полями карточки."""
        keys = []
        for _, _, options in found:
            keys.extend(k for k in options if k not in keys)
        fields = QgsFields()
        fields.append(QgsField("interactionId", QMetaType.Type.LongLong))
        for key in keys:
            fields.append(QgsField(key, QMetaType.Type.QString))
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
        return dest_id


class NspdEnterpriseAlgorithm(NspdParcelsAlgorithm):
    """Граница предприятия — участок ЕГРН по кадастровому номеру (один или несколько через запятую)."""
    CAD = "CAD"

    def createInstance(self):
        return NspdEnterpriseAlgorithm()

    def name(self):
        return "nspd_parcel"

    def displayName(self):
        return self.tr("Участок предприятия с НСПД по кадастровому номеру")

    def shortHelpString(self):
        return self.tr(
            "Граница земельного участка (или нескольких — номера через запятую) с публичной "
            "кадастровой карты НСПД по кадастровому номеру: слой для границы предприятия. "
            "НСПД доступен только из России; участок без координат границ не загрузится.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(
            self.CAD, self.tr("Кадастровые номера участков предприятия")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("Участок предприятия"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):
        numbers = core.cad_numbers(self.parameterAsString(parameters, self.CAD, context))
        if not numbers:
            raise QgsProcessingException(self.tr(
                "Введите кадастровый номер участка, например 23:43:0000000:123."))
        found, missing = [], []
        try:
            for number in numbers:
                part = nspd.parcels(nspd.find(number, feedback))
                if feedback.isCanceled():
                    return {}
                if part:
                    found.extend(part)
                else:
                    missing.append(number)
        except nspd.NspdError as e:
            raise QgsProcessingException(str(e))
        if not found:
            raise QgsProcessingException(self.tr(
                "НСПД не нашёл участок с границами по номеру {}. Проверьте номер: у участка без "
                "координат границ в ЕГРН контура нет.").format(", ".join(missing)))
        if missing:
            feedback.pushWarning(self.tr("Не найдены на НСПД (или без границ): {}").format(
                ", ".join(missing)))
        # СК — проекта, если она в метрах, иначе зона UTM по месту
        out_crs = context.project().crs() if context.project() else QgsCoordinateReferenceSystem()
        if not out_crs.isValid() or not core.true_metres(out_crs):
            shape = QgsGeometry(found[0][1])
            shape.transform(QgsCoordinateTransform(WEB_MERCATOR, WGS84, context.transformContext()))
            out_crs = core.working_crs(WGS84, shape)
        dest_id = self._write(found, parameters, context, out_crs)
        feedback.pushInfo(self.tr("Загружено участков: {}").format(len(found)))
        return {self.OUTPUT: dest_id, "COUNT": len(found), "MISSING": ", ".join(missing)}
