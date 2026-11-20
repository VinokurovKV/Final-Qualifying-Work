"""Необходимые условия IPv4 GRE по объявленным базе и дельте, без эмуляции DUT."""

import ipaddress
import re
import xml.etree.ElementTree as ET

from .junos_xml import KEYED, NETCONF_NS, PARAMETER, _normalized, _parse
from .requirements import Requirement
from .addressing import check_addressing


def declared_configuration(base_xml: str, delta_xml: str) -> ET.Element:
    """Упрощённое объединение известных настроек; не эффективный running configuration."""
    result = ET.Element("configuration")

    def merge(target: ET.Element, source: ET.Element) -> None:
        for change in source:
            name = change.findtext("name") if change.tag in KEYED else None
            candidates = [node for node in target if node.tag == change.tag]
            if change.tag in KEYED:
                candidates = [node for node in candidates if node.findtext("name") == name]
            elif change.tag == "next-hop":
                candidates = [node for node in candidates if (node.text or "").strip() == (change.text or "").strip()]
            existing = candidates[0] if candidates else None
            operation = change.get("operation", change.get(f"{{{NETCONF_NS}}}operation", "merge"))
            if operation not in {"merge", "replace", "delete", "remove"}:
                raise ValueError(f"Операция {operation} не поддержана локальной моделью подготовки.")
            if operation in {"replace", "delete", "remove"} and existing is not None:
                target.remove(existing)
                existing = None
            if operation in {"delete", "remove"}:
                continue
            if existing is None:
                existing = ET.SubElement(target, change.tag)
            if len(change):
                merge(existing, change)
            else:
                existing.text = (change.text or "").strip()

    for xml in (base_xml, delta_xml):
        root = _normalized(_parse(xml))
        if root.tag != "configuration":
            raise ValueError("Подготовка ожидает корень configuration.")
        merge(result, root)
    return result


def check_preparation(base: dict, delta_xml: str, requirements: list[Requirement]) -> dict:
    """Проверяем необходимые условия профиля, а не полноту сценария или RFC-поведение."""
    gre_requirements = [req for req in requirements if
                        "GRE" in {tag.upper() for tag in req.tagCodes} or
                        re.search(r"\bGRE\b", req.description.text, re.I)]
    if not gre_requirements:
        return {"status": "not_applicable", "profile": "ipv4_gre"}
    needs_encapsulation = any(re.search(r"\b(?:формир\w*|инкапсуляци\w*|encapsulat(?:e|es|ed|ing|ion|ions)|transmit(?:s|ted|ting)?)\b",
                                        req.description.text, re.I) for req in gre_requirements)
    view = declared_configuration(base["configuration_xml"], delta_xml)
    interfaces = [node for node in view.findall("interfaces/interface") if node.find("disable") is None]
    physical = [node for node in interfaces if node.findtext("name") in base["available_physical_interfaces"]]
    issues = []
    if not any(unit.find("disable") is None and unit.findall("family/inet/address")
               for node in physical for unit in node.findall("unit")):
        issues.append("В базе + дельте отсутствует IPv4-адресация физических интерфейсов профиля underlay.")
    units = [(f"{node.findtext('name')}.{unit.findtext('name')}", unit)
             for node in interfaces if (node.findtext("name") or "").startswith("gr-")
             for unit in node.findall("unit") if unit.find("disable") is None]
    if not units:
        issues.append("В базе + дельте нет настроенного GRE-интерфейса с unit. "
                      "Отсутствие переключателей проверяемых полей не отменяет настройку самого туннеля.")
    encapsulation_paths = []
    parameterized_path = False
    for name, unit in units:
        if not (unit.findtext("tunnel/source", "").strip() and unit.findtext("tunnel/destination", "").strip()):
            issues.append(f"В базе + дельте для {name} отсутствует tunnel/source или tunnel/destination.")
        if unit.find("family/inet") is None:
            issues.append(f"В базе + дельте для {name} отсутствует family inet для IPv4 GRE.")
        connected = False
        for address in unit.findall("family/inet/address"):
            value = address.findtext("name", "").strip()
            if PARAMETER.search(value):
                connected = True
                parameterized_path = True
            elif value:
                try:
                    connected |= ipaddress.IPv4Interface(value).network.num_addresses > 1
                except ValueError as error:
                    issues.append(f"Некорректный адрес GRE-интерфейса {name}: {error}")
        routed = any(route.find("discard") is None and route.find("reject") is None and
                     any((hop.text or "").strip() == name for hop in route.findall("next-hop"))
                     for route in view.findall("routing-options/static/route"))
        if connected or routed:
            encapsulation_paths.append(name)
    if needs_encapsulation and not encapsulation_paths:
        issues.append("Для проверки формирования GRE в базе + дельте нет пути отправки IPv4 в туннель: "
                      "нужна пригодная подключённая сеть GRE либо маршрут через логический GRE-интерфейс.")
    if issues:
        raise ValueError("Не выполнены необходимые условия подготовки IPv4 GRE:\n- " + "\n- ".join(issues))
    addressing = check_addressing(view, base["available_physical_interfaces"])
    return {
        "status": "pending_address_bindings" if addressing["status"] == "pending_parameters"
                  else "passed_necessary_conditions", "profile": "ipv4_gre",
        "scope": "declared_base_plus_delta", "requires_encapsulation": needs_encapsulation,
        "gre_units": [name for name, _ in units], "encapsulation_paths": encapsulation_paths,
        "address_bindings_require_verification": parameterized_path or bool(PARAMETER.search(ET.tostring(view, encoding="unicode"))),
        "addressing": addressing,
        "full_scenario_sufficiency_confirmed": False, "device_validation_performed": False,
    }
