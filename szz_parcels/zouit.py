"""XML о границе СЗЗ для внесения в ЕГРН и адрес из выписки ЕГРН.

Формат — схема Росреестра interact_entry_boundaries_v02 (urn …/interact-entry-boundaries/2.0.1),
по образцу пользователя (СЗЗ ПС 330 кВ Мончегорск, 2026-10-27): вид границы 6 (ЗОУИТ), вид зоны
218020030005 (СЗЗ), координаты в МСК (sk_code «код субъекта.зона»), метод определения —
аналитический (692006000000), точность 0,1 м, «Закрепление отсутствует». Тесты сверяют файл со
схемой (XSD в tests/xsd).
"""

import uuid
import xml.etree.ElementTree as ET

NS = "urn://x-artefacts-rosreestr-ru/interact/interact-entry-boundaries/2.0.1"
ADR = "urn://x-artefacts-rosreestr-ru/commons/complex-types/address-city/2.0.1"
SPA = "urn://x-artefacts-rosreestr-ru/commons/complex-types/entity-spatial/2.0.1"
ZONE_SZZ = "218020030005"
BOUNDARY_ZOUIT = "6"
GEOPOINT_OPRED = "692006000000"  # аналитический метод
DELTA = "0.1"
ZACREP = "Закрепление отсутствует"
NAME_BY_DOC = "Санитарно-защитная зона"
# ограничения в СЗЗ — п. 5 Правил установления СЗЗ (ПП РФ от 03.03.2018 № 222), как в образце
RESTRICT = (
    "В границах санитарно-защитной зоны не допускается использования земельных участков в целях: "
    "- размещения жилой застройки, объектов образовательного и медицинского назначения, "
    "спортивных сооружений открытого типа, организаций отдыха детей и их оздоровления, зон "
    "рекреационного назначения и для ведения садоводства; - размещения объектов для производства "
    "и хранения лекарственных средств, объектов пищевых отраслей промышленности, оптовых складов "
    "продовольственного сырья и пищевой продукции, комплексов водопроводных сооружений для "
    "подготовки и хранения питьевой воды, использования земельных участков в целях производства, "
    "хранения и переработки сельскохозяйственной продукции, предназначенной для дальнейшего "
    "использования в качестве пищевой продукции, если химическое, физическое и (или) "
    "биологическое воздействие объекта, в отношении которого установлена санитарно-защитная "
    "зона, приведет к нарушению качества и безопасности таких средств, сырья, воды и продукции в "
    "соответствии с установленными к ним требованиями.")
# элементы адреса в порядке схемы (AdrCi2:AddressCity); составные — с типом и названием
ADDRESS_ORDER = ("fias", "okato", "kladr", "oktmo", "postal_code", "region", "district", "city",
                 "urban_district", "soviet_village", "locality")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(node, name):
    return next((c for c in node if _local(c.tag) == name), None)


def read_extract(path):
    """Адрес и номер из XML-выписки ЕГРН о земельном участке.

    {"cad": номер или None, "readable": адрес строкой или None,
     "location": [(элемент, текст или [(подэлемент, текст)])] в порядке схемы} — или None,
    если в файле нет address_location (не выписка о ЗУ, PDF и т. п.)."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    address = next((e for e in root.iter() if _local(e.tag) == "address_location"), None)
    if address is None:
        return None
    cad = next((e.text.strip() for e in root.iter()
                if _local(e.tag) == "cad_number" and e.text and e.text.strip()), None)
    readable = next((e.text.strip() for e in address.iter()
                     if _local(e.tag) == "readable_address" and e.text), None)
    level = next((e for e in address.iter() if _local(e.tag) == "level_settlement"), None)
    location = []
    if level is not None:
        for name in ADDRESS_ORDER:
            node = _child(level, name)
            if node is None:
                continue
            if name == "region":  # в выписке {code, value}, в схеме — код
                code = _child(node, "code")
                if code is not None and code.text:
                    location.append((name, code.text.strip()))
            elif len(node):
                parts = [(_local(c.tag), c.text.strip()) for c in node if c.text and c.text.strip()]
                if parts:
                    location.append((name, parts))
            elif node.text and node.text.strip():
                location.append((name, node.text.strip()))
    return {"cad": cad, "readable": readable, "location": location}


def _sub(parent, ns, name, text=None):
    node = ET.SubElement(parent, "{%s}%s" % (ns, name))
    if text is not None:
        node.text = str(text)
    return node


def write(path, polygons, sk_code, cadastral_district, region, protected_object="",
          authority="", start_date="", location=None, guid=None):
    """Записать XML о границе СЗЗ. polygons — core.szz_rings в МСК; location — из read_extract
    (без него — только код субъекта region). Возвращает guid документа."""
    guid = guid or str(uuid.uuid4())
    ET.register_namespace("", NS)
    ET.register_namespace("AdrCi2", ADR)
    ET.register_namespace("EnSpa2", SPA)
    root = ET.Element("{%s}interact_entry_boundaries" % NS, {"guid": guid, "version": "02"})
    boundary = _sub(_sub(root, NS, "information_registry_boundaries"), NS,
                    "information_registry_boundary")
    _sub(boundary, NS, "type_boundary", BOUNDARY_ZOUIT)
    name = NAME_BY_DOC + (" " + protected_object if protected_object else "")
    _sub(boundary, NS, "name_object", name)
    _sub(boundary, NS, "all_border_or_part_border", "1")
    zones = _sub(_sub(_sub(boundary, NS, "information_boundary"), NS, "zones_and_territories"),
                 NS, "establishment_zones_and_territories")
    obj = _sub(zones, NS, "object_zones_and_territories")
    _sub(obj, NS, "cadastral_district", cadastral_district)
    _sub(obj, NS, "name_by_doc", NAME_BY_DOC)
    _sub(obj, NS, "type_zone", ZONE_SZZ)
    if authority:
        _sub(obj, NS, "authority_decision", authority)
    place = _sub(_sub(obj, NS, "locations"), NS, "location")
    items = location or [("region", region)]
    if not any(key == "region" for key, _ in items):
        items = sorted(items + [("region", region)], key=lambda kv: ADDRESS_ORDER.index(kv[0]))
    for key, value in items:
        if isinstance(value, list):
            node = _sub(place, ADR, key)
            for sub, text in value:
                _sub(node, ADR, sub, text)
        else:
            _sub(place, ADR, key, value)
    parameter = _sub(zones, NS, "zone_parameter")
    _sub(parameter, NS, "content_restrict_encumbrances", RESTRICT)
    if protected_object:
        _sub(parameter, NS, "protected_object", protected_object)
    if start_date:
        period = _sub(parameter, NS, "period")
        _sub(period, NS, "start_date", start_date)
        _sub(period, NS, "indefinitely", "true")
    contours = _sub(_sub(zones, NS, "contours_location"), SPA, "contours")
    for rings in polygons:
        entity = _sub(_sub(contours, SPA, "contour"), SPA, "entity_spatial")
        _sub(entity, SPA, "sk_code", sk_code)
        elements = _sub(entity, SPA, "spatials_elements")
        for ring in rings:  # внешнее кольцо и дыры — элементы одного контура
            element = _sub(elements, SPA, "spatial_element")
            _sub(element, SPA, "type_unit", "01")
            ordinates = _sub(element, SPA, "ordinates")
            for number, x, y in ring:
                ordinate = _sub(ordinates, SPA, "ordinate")
                _sub(ordinate, SPA, "x", "{:.2f}".format(x))
                _sub(ordinate, SPA, "y", "{:.2f}".format(y))
                _sub(ordinate, SPA, "ord_nmb", number)
                _sub(ordinate, SPA, "num_geopoint", number)
                _sub(ordinate, SPA, "geopoint_zacrep", ZACREP)
                _sub(ordinate, SPA, "geopoint_opred", GEOPOINT_OPRED)
                _sub(ordinate, SPA, "delta_geopoint", DELTA)
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return guid
