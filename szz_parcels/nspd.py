"""Земельные участки с публичной кадастровой карты НСПД (Росреестр) по области.

Запрос — тот же, что делает карта nspd.gov.ru при выборе объектов в полигоне:
POST /api/geoportal/v1/intersects, категория 36368 «Земельные участки из ЕГРН».
Область — GeoJSON в EPSG:4326, ответ — участки в EPSG:3857 с карточкой в properties.options.

Защита НСПД пропускает только запросы с полным набором заголовков браузера, а
QgsNetworkAccessManager подменяет User-Agent своим, поэтому запрос идёт через свой
QNetworkAccessManager (заголовки — как в модуле «Поиск адреса», проверены вживую).
НСПД доступен только из России. Сертификаты Минцифры — в certs/ (у кого их нет в системе).
"""

import json
import os
import time

from qgis.core import QgsGeometry, QgsNetworkAccessManager, QgsPointXY, QgsRectangle
from qgis.PyQt.QtCore import QByteArray, QEventLoop, QTimer, QUrl
from qgis.PyQt.QtNetwork import (
    QNetworkAccessManager,
    QNetworkRequest,
    QSsl,
    QSslCertificate,
)

URL = "https://nspd.gov.ru/api/geoportal/v1/intersects?typeIntersect=fullObject"
CATEGORY = 36368  # «Земельные участки из ЕГРН»
CA_BUNDLE = os.path.join(os.path.dirname(__file__), "certs", "nspd-ca-bundle.pem")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HEADERS = (
    (b"User-Agent", USER_AGENT.encode()),
    (b"Accept", b"application/json, text/plain, */*"),
    (b"Accept-Language", b"ru-RU,ru,en-US,en"),
    (b"Content-Type", b"application/json"),
    (b"Referer", b"https://nspd.gov.ru/map?thematic=PKK"),
    (b"Origin", b"https://nspd.gov.ru"),
    (b"Cache-Control", b"no-cache"),
    (b"Pragma", b"no-cache"),
    (b"Sec-Fetch-Dest", b"empty"),
    (b"Sec-Fetch-Mode", b"cors"),
    (b"Sec-Fetch-Site", b"same-origin"),
    (b"sec-ch-ua", b'"Chromium";v="140", "Google Chrome";v="140", "Not:A-Brand";v="24"'),
    (b"sec-ch-ua-mobile", b"?0"),
    (b"sec-ch-ua-platform", b'"macOS"'),
)
# 597 и 429 — защита отбивает всплеск, следом пропускает; 500 — обычно «слишком большая область»
RETRY_STATUSES = (429, 502, 503, 504, 597)
TOO_BIG_STATUSES = (413, 500)
MAX_ATTEMPTS = 3
RETRY_DELAY_S = 3.0
TIMEOUT_MS = 60000
MAX_SPLIT = 4  # область делится на 4 части не глубже — до 256 запросов


class NspdError(Exception):
    """Понятная пользователю причина, почему участки не загрузились."""


def payload(area_wgs84):
    """Тело запроса: область — GeoJSON в EPSG:4326."""
    geometry = json.loads(area_wgs84.asJson(7))
    return {"categories": [{"id": CATEGORY}],
            "geom": {"type": "FeatureCollection",
                     "features": [{"type": "Feature", "geometry": geometry, "properties": {}}]}}


def _ca_certificates():
    if not os.path.exists(CA_BUNDLE):
        return []
    return QSslCertificate.fromPath(CA_BUNDLE, QSsl.EncodingFormat.Pem)


def post(body, feedback=None):
    """(код HTTP или 0 при обрыве, тело ответа). Один запрос, без повторов."""
    manager = QNetworkAccessManager()
    proxy = QgsNetworkAccessManager.instance().proxy()
    manager.setProxy(proxy)
    request = QNetworkRequest(QUrl(URL))
    for name, value in HEADERS:
        request.setRawHeader(name, value)
    http2 = getattr(QNetworkRequest.Attribute, "Http2AllowedAttribute",
                    getattr(QNetworkRequest.Attribute, "HTTP2AllowedAttribute", None))
    if http2 is not None:
        request.setAttribute(http2, False)  # с HTTP/2 НСПД зависает, см. «Поиск адреса»
    ssl = request.sslConfiguration()
    ssl.setCaCertificates(ssl.caCertificates() + _ca_certificates())
    request.setSslConfiguration(ssl)

    reply = manager.post(request, QByteArray(json.dumps(body).encode("utf-8")))
    loop = QEventLoop()
    reply.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(reply.abort)
    timer.start(TIMEOUT_MS)
    tick = QTimer()  # отмена из окна хода расчёта
    tick.timeout.connect(lambda: feedback is not None and feedback.isCanceled() and reply.abort())
    tick.start(200)
    if not reply.isFinished():
        loop.exec()
    tick.stop()
    timer.stop()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
    data = bytes(reply.readAll()) if reply.isOpen() else b""  # оборванный ответ не читается
    reply.deleteLater()
    return int(status), data


# Тесты подменяют запрос и паузу между повторами
TRANSPORT = post
SLEEP = time.sleep


def _request(area_wgs84, feedback):
    """Ответ НСПД по одной области с повторами. None — область слишком большая."""
    status = 0
    for attempt in range(MAX_ATTEMPTS):
        if feedback is not None and feedback.isCanceled():
            return []
        status, data = TRANSPORT(payload(area_wgs84), feedback)
        if status == 200:
            try:
                return json.loads(data.decode("utf-8")).get("features") or []
            except ValueError:
                raise NspdError("НСПД прислал ответ, который не удалось прочитать. "
                                "Повторите загрузку через минуту.")
        if status == 204:
            return []
        if status in TOO_BIG_STATUSES:
            return None
        if status in RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            SLEEP(RETRY_DELAY_S)
            continue
        break
    if status == 0:
        raise NspdError("НСПД не отвечает. Он доступен только из России: проверьте интернет; "
                        "если включён VPN на зарубежный адрес — отключите его.")
    if status in (403, 597, 429):
        raise NspdError("НСПД временно не пускает запросы (защита от частых обращений). "
                        "Подождите минуту и нажмите «Загрузить с НСПД» ещё раз.")
    raise NspdError("НСПД ответил ошибкой {}. Повторите загрузку позже.".format(status))


def _quarters(area):
    box = area.boundingBox()
    cx, cy = box.center().x(), box.center().y()
    for x1, y1, x2, y2 in ((box.xMinimum(), box.yMinimum(), cx, cy), (cx, box.yMinimum(), box.xMaximum(), cy),
                           (box.xMinimum(), cy, cx, box.yMaximum()), (cx, cy, box.xMaximum(), box.yMaximum())):
        part = area.intersection(QgsGeometry.fromRect(QgsRectangle(x1, y1, x2, y2)))
        if part is not None and not part.isEmpty() and part.area() > 0:
            yield part


def fetch(area_wgs84, feedback=None, depth=0):
    """Все участки, пересекающие область (EPSG:4326). Большая область — по частям."""
    features = _request(area_wgs84, feedback)
    if features is not None:
        return features
    if depth >= MAX_SPLIT:
        raise NspdError("Область слишком большая для НСПД даже по частям. "
                        "Уменьшите расстояние поиска.")
    result = []
    for part in _quarters(area_wgs84):
        result.extend(fetch(part, feedback, depth + 1))
    return result


def _ring(points):
    return [QgsPointXY(float(x), float(y)) for x, y, *rest in points]


def geometry(data):
    """Геометрия участка из GeoJSON ответа (EPSG:3857) или None."""
    if not data:
        return None
    kind, coordinates = data.get("type"), data.get("coordinates")
    if kind == "Polygon":
        return QgsGeometry.fromPolygonXY([_ring(r) for r in coordinates])
    if kind == "MultiPolygon":
        return QgsGeometry.fromMultiPolygonXY([[_ring(r) for r in p] for p in coordinates])
    return None


def value(raw):
    """Значение карточки как строка: список — через «; », пустое — None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        raw = "; ".join(str(v) for v in raw if v not in (None, ""))
        return raw or None
    return str(raw)


def parcels(features):
    """[(id, геометрия 3857, {поле: значение})] без повторов и без участков без границ."""
    seen = set()
    result = []
    for feature in features:
        properties = feature.get("properties") or {}
        options = properties.get("options") or {}
        key = properties.get("interactionId") or options.get("cad_num") or id(feature)
        if key in seen:
            continue
        seen.add(key)
        shape = geometry(feature.get("geometry"))
        if shape is None or shape.isEmpty():
            continue
        result.append((properties.get("interactionId"), shape,
                       {k: value(v) for k, v in options.items()}))
    return result
