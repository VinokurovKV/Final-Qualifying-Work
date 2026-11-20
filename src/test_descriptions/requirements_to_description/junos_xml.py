"""Локальная проверка ограниченного подмножества Junos XML, без доступа к DUT."""

import copy
import hashlib
import ipaddress
import re
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path


DEFAULT_TEST_BASE_PATH = Path(__file__).resolve().parents[3] / "configurations/BASE_RFC2784.xml"
PARAMETER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
BASE_RPC_TEMPLATE = Path(__file__).with_name("prompts") / "base_configuration_rpc.xml"

# Это проверенный локальный профиль, не XSD всей Junos и не подтверждение поддержки DUT.
CHILDREN = {
    "configuration": {"interfaces", "routing-options"},
    "configuration/interfaces": {"interface"},
    "configuration/interfaces/interface": {"name", "description", "unit", "disable"},
    "configuration/interfaces/interface/unit": {"name", "description", "tunnel", "family"},
    "configuration/interfaces/interface/unit/tunnel": {"source", "destination", "ttl"},
    "configuration/interfaces/interface/unit/family": {"inet"},
    "configuration/interfaces/interface/unit/family/inet": {"address", "mtu"},
    "configuration/interfaces/interface/unit/family/inet/address": {"name", "arp"},
    "configuration/interfaces/interface/unit/family/inet/address/arp": {"name", "mac", "publish"},
    "configuration/routing-options": {"static"},
    "configuration/routing-options/static": {"route"},
    "configuration/routing-options/static/route": {"name", "next-hop", "discard", "reject", "preference"},
}
REPEATED = {"interface", "unit", "address", "arp", "route", "next-hop"}
KEYED = {"interface", "unit", "address", "arp", "route"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(text: str) -> ET.Element:
    if len(text.encode("utf-8")) > 2_000_000 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML слишком велик или содержит запрещённый DTD/ENTITY.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"Некорректный XML: {error}") from error


def _normalized(root: ET.Element) -> ET.Element:
    root = copy.deepcopy(root)
    for node in root.iter():
        node.tag = _local(node.tag)
    return root


def wrap_base_configuration(xml: str) -> str:
    """Формат файла системы: последовательность RPC, без их выполнения."""
    if _local(_parse(xml).tag) != "configuration":
        raise ValueError("Для обёртки нужен один блок configuration.")
    template = BASE_RPC_TEMPLATE.read_text(encoding="utf-8")
    if template.count("{{CONFIGURATION}}") != 1:
        raise ValueError("В шаблоне RPC должен быть один маркер CONFIGURATION.")
    fragment = re.sub(r"^\s*<\?xml[^?]*\?>", "", xml, count=1).strip()
    result = template.replace("{{CONFIGURATION}}", textwrap.indent(fragment, " " * 10))
    _parse("<documents>" + result + "</documents>")
    return result


def load_base_configuration(path: Path = DEFAULT_TEST_BASE_PATH) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        root = _parse(text)
    except ValueError:
        # Файл может быть записью нескольких RPC. Извлекаем данные, ничего не выполняем.
        fragment = re.sub(r"^\s*<\?xml[^?]*\?>", "", text, count=1)
        root = _parse("<documents>" + fragment + "</documents>")
        if not len(root) or any(_local(node.tag) != "rpc" for node in root):
            raise ValueError("База должна содержать configuration или последовательность RPC.")
    root = _normalized(root)
    configurations = list(root.iter("configuration"))
    if len(configurations) != 1:
        raise ValueError("В базовом файле должен быть ровно один элемент configuration.")
    configuration = configurations[0]
    operations = [node.tag for rpc in root.iter("rpc") for node in rpc if node.tag != "edit-config"]
    warnings = [
        "Локально не выполнялись Junos schema validation, NETCONF validate, commit и проверка на DUT.",
        "Поддержка конкретных проверок RFC и аппаратных режимов EX4650 требует проверки на устройстве.",
        "Активация GRE tunnel-services и её синтаксис для установленного Junos требуют подтверждения; "
        "неподтверждённые chassis-настройки в Delta XML не включаются.",
    ]
    if root.tag != "configuration":
        warnings.append("База извлечена из RPC-файла подготовки, а не из полного снимка running configuration. "
                        "Атрибуты операций сохранены; эффективное состояние устройства локально не вычислялось.")
    version = configuration.findtext("version")
    if not version:
        warnings.append("Версия Junos OS не указана в базе; совместимость конфигурации с конкретным релизом не подтверждена.")
    # Не передаём модели учётные данные, системные ключи и идентификатор устройства из DHCP.
    excluded = []
    for parent in configuration.iter():
        for child in list(parent):
            if child.tag in {"system", "vendor-id"} or any(token in child.tag for token in ("password", "secret", "private-key")):
                excluded.append(child.tag)
                parent.remove(child)
    available = [node.findtext("name") for node in configuration.findall("interfaces/interface")
                 if re.fullmatch(r"(?:xe|ge|et)-\d+/\d+/\d+", node.findtext("name", ""))
                 and node.find("disable") is None]
    return {
        "id": path.stem, "source": str(path), "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "device_model": "Juniper EX4650", "junos_version": version,
        "configuration_xml": ET.tostring(configuration, encoding="unicode"),
        "is_template": bool(PARAMETER.search(ET.tostring(configuration, encoding="unicode"))),
        "available_physical_interfaces": available,
        "excluded_context_elements": sorted(set(excluded)), "ignored_rpc_operations": operations,
        "limitations": warnings,
    }


def _tree(node: ET.Element) -> dict:
    return {"tag": node.tag, "attributes": dict(node.attrib), "value": (node.text or "").strip(),
            "children": [_tree(child) for child in node]}


def parse_junos_xml(xml: str, parameters: dict[str, str], port_assignments: list[dict],
                    base: dict, port_count: int, limitations: list[str], *, scope: str) -> dict:
    if scope not in {"delta_changes_only", "rfc_shared_base"}:
        raise ValueError("Неизвестное назначение Junos XML.")
    root = _parse(xml)
    for node in root.iter():
        namespace = node.tag[1:].split("}")[0] if node.tag.startswith("{") else ""
        if namespace not in ("", "http://xml.juniper.net/xnm/1.1/xnm"):
            raise ValueError(f"Неподтверждённое пространство имён конфигурации: {namespace}")
    root = _normalized(root)
    if root.tag != "configuration":
        raise ValueError("Junos XML должен содержать только configuration, без RPC/commit.")
    used_parameters = set(PARAMETER.findall(xml))
    if used_parameters != set(parameters):
        raise ValueError("Несогласованные параметры XML: необъявленные: "
                         + ", ".join(sorted(used_parameters - set(parameters)))
                         + "; неиспользуемые: " + ", ".join(sorted(set(parameters) - used_parameters)))
    if any(not value.strip() for value in parameters.values()):
        raise ValueError("Каждый параметр XML должен иметь непустое описание.")

    structure_errors = []

    def check_paths(node: ET.Element, path: str) -> None:
        if path == "configuration/routing-options/static/route/next-hop" and len(node):
            structure_errors.append("Некорректный Junos XML: next-hop должен содержать адрес или имя интерфейса "
                                    "непосредственно текстом, без вложенных тегов. "
                                    "Пример: <next-hop>${NEXT_HOP_IP}</next-hop>.")
            return
        for child in node:
            if child.tag not in CHILDREN.get(path, set()):
                if path == "configuration/interfaces/interface" and child.tag == "tunnel":
                    structure_errors.append("Некорректный Junos XML: tunnel должен находиться внутри interface/unit, "
                                            "а не непосредственно в interface. source и destination внутри tunnel "
                                            "содержат IP текстом, без вложенного name.")
                else:
                    details = "; дочерние элементы: " + ", ".join(item.tag for item in child) if len(child) else ""
                    structure_errors.append(f"Неподтверждённый локальным профилем XML-путь: {path}/{child.tag}{details}")
            else:
                check_paths(child, path + "/" + child.tag)

    check_paths(root, "configuration")
    if structure_errors:
        raise ValueError("\n".join(structure_errors))

    def validate(node: ET.Element, path: str) -> None:
        if len(node) and (node.text or "").strip():
            raise ValueError(f"Смешанный XML-контент не поддерживается: {path}")
        operation = None
        for key, value in node.attrib.items():
            if scope == "rfc_shared_base":
                raise ValueError("Общая база должна описывать состояние без XML-операций изменения.")
            if key not in ("operation", f"{{{NETCONF_NS}}}operation") or value not in ("merge", "replace", "delete", "remove"):
                raise ValueError(f"Неподтверждённый XML-атрибут: {path}/@{key}")
            if operation is not None or node.tag not in KEYED:
                raise ValueError(f"Операция допустима только для именованного объекта: {path}")
            operation = value
        seen = set()
        keys = set()
        for child in node:
            if child.tag in seen and child.tag not in REPEATED:
                raise ValueError(f"Повтор одиночного XML-поля: {path}/{child.tag}")
            seen.add(child.tag)
            if child.tag in KEYED:
                name = child.findtext("name", "").strip()
                if not name or child[0].tag != "name" or (child.tag, name) in keys:
                    raise ValueError(f"Некорректный или повторный идентификатор объекта: {path}/{child.tag}")
                keys.add((child.tag, name))
            if child.tail and child.tail.strip():
                raise ValueError(f"Смешанный XML-контент: {path}")
            validate(child, path + "/" + child.tag)
        value = (node.text or "").strip()
        if not len(node) and node.tag not in {"configuration", "inet", "disable", "publish", "discard", "reject"} and not value:
            if node.tag not in CHILDREN:
                raise ValueError(f"Пустое XML-значение: {path}")
        if node.tag in {"disable", "publish", "discard", "reject"} and value:
            raise ValueError(f"Ожидался пустой флаг: {path}")
        if value and not PARAMETER.search(value):
            try:
                if node.tag in {"source", "destination"} or (node.tag == "name" and path.rsplit("/", 2)[-2] == "arp"):
                    ipaddress.IPv4Address(value)
                elif node.tag == "name" and path.rsplit("/", 2)[-2] == "address":
                    ipaddress.IPv4Interface(value)
                elif node.tag == "name" and path.rsplit("/", 2)[-2] == "route":
                    ipaddress.IPv4Network(value)
                elif node.tag in {"ttl", "mtu", "preference"} or (node.tag == "name" and path.endswith("/unit/name")):
                    number = int(value)
                    if number < 0 or (node.tag == "ttl" and not 1 <= number <= 255) or (node.tag == "mtu" and number == 0):
                        raise ValueError("Значение вне допустимого диапазона")
                elif node.tag == "next-hop":
                    try:
                        ipaddress.IPv4Address(value)
                    except ValueError:
                        if not re.fullmatch(r"(?:gr|xe|ge|et)-\d+/\d+/\d+\.\d+", value):
                            raise ValueError("Ожидался IP или логический интерфейс")
                elif node.tag == "mac" and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|[0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2}", value):
                    raise ValueError("MAC должен иметь формат xx:xx:xx:xx:xx:xx или xxxx.xxxx.xxxx")
            except ValueError as error:
                raise ValueError(f"Некорректное значение {path}: {value} ({error}). "
                                 "Неизвестное значение задайте как ${PARAMETER}.") from error

    validate(root, "configuration")
    ports = [item["port"] for item in port_assignments]
    names = [item["interface"] for item in port_assignments]
    if len(ports) != len(set(ports)) or len(names) != len(set(names)) or any(not 1 <= port <= port_count for port in ports):
        raise ValueError("Назначения портов повторяются или выходят за пределы топологии.")
    if set(names) - set(base["available_physical_interfaces"]):
        raise ValueError("Физический интерфейс назначения отсутствует в доступных портах базовой конфигурации.")
    interfaces, tunnels, routes, arp = [], [], [], []
    for interface in root.findall("interfaces/interface"):
        name = interface.findtext("name")
        if not name.startswith("gr-") and name not in names:
            raise ValueError(f"Изменяемый интерфейс {name} не связан с физической топологией.")
        if name.startswith("gr-") and name != "gr-0/0/0":
            raise ValueError("В локальном профиле подтверждено имя GRE-интерфейса gr-0/0/0.")
        item = {"name": name, "attributes": dict(interface.attrib), "description": interface.findtext("description"),
                "disabled": interface.find("disable") is not None, "units": []}
        for unit in interface.findall("unit"):
            unit_name = unit.findtext("name")
            addresses = [address.findtext("name") for address in unit.findall("family/inet/address")]
            item["units"].append({"name": unit_name, "attributes": dict(unit.attrib), "ipv4_addresses": addresses,
                                  "mtu": unit.findtext("family/inet/mtu")})
            tunnel = unit.find("tunnel")
            if tunnel is not None:
                if not name.startswith("gr-") or (scope == "rfc_shared_base" and
                                                  (not tunnel.findtext("source") or not tunnel.findtext("destination"))):
                    raise ValueError("Для GRE tunnel необходимы gr-интерфейс, source и destination.")
                tunnels.append({"interface": f"{name}.{unit_name}", "source": (tunnel.findtext("source") or "").strip() or None,
                                "destination": (tunnel.findtext("destination") or "").strip() or None, "ttl": tunnel.findtext("ttl")})
            for address in unit.findall("family/inet/address"):
                for entry in address.findall("arp"):
                    operation = entry.get("operation", entry.get(f"{{{NETCONF_NS}}}operation", "merge"))
                    if operation not in ("delete", "remove") and not entry.findtext("mac"):
                        raise ValueError("Для добавления static ARP необходимо значение mac.")
                    arp.append({"interface": f"{name}.{unit_name}", "local_address": address.findtext("name"),
                                "address": entry.findtext("name"), "mac": entry.findtext("mac"),
                                "attributes": dict(entry.attrib), "publish": entry.find("publish") is not None})
        interfaces.append(item)
    for route in root.findall("routing-options/static/route"):
        if sum([bool(route.findall("next-hop")), route.find("discard") is not None, route.find("reject") is not None]) > 1:
            raise ValueError("Маршрут не может одновременно иметь next-hop и discard/reject.")
        routes.append({"destination": route.findtext("name"), "next_hops": [el.text for el in route.findall("next-hop")],
                       "attributes": dict(route.attrib), "discard": route.find("discard") is not None,
                       "reject": route.find("reject") is not None, "preference": route.findtext("preference")})
    local_limitations = list(base["limitations"])
    if scope == "rfc_shared_base" and tunnels and not any(item["units"] and any(unit["ipv4_addresses"] for unit in item["units"])
                           for item in interfaces if not item["name"].startswith("gr-")):
        local_limitations.append("XML не задаёт IPv4-адресацию физического underlay. "
                                 "Наличие необходимой адресации и достижимость GRE endpoint из базы не подтверждены.")
    identity = (
        {"base_configuration_id": base["id"], "base_configuration_sha256": base["source_sha256"]}
        if scope == "delta_changes_only" else
        {"configuration_id": base["id"], "inventory_source_sha256": base["source_sha256"],
         "configuration_sha256": hashlib.sha256(xml.encode()).hexdigest()}
    )
    return {
        **identity,
        "device_model": base["device_model"], "junos_version": base["junos_version"],
        "validation": {"well_formed_xml": True, "local_subset_checked": True, "junos_schema_validated": False,
                       "device_support_confirmed": False, "netconf_validate_performed": False, "applied": False,
                       "effective_configuration_computed": False, "semantic_sufficiency_confirmed": False,
                       "test_execution_performed": False},
        "scope": scope,
        "is_template": bool(used_parameters), "ready_to_apply": False,
        "default_operation": "merge" if scope == "delta_changes_only" else None,
        "parameters": parameters, "port_assignments": port_assignments, "port_assignment_status": "proposed",
        "documented_platform_features": [
            {"feature": feature, "platform": "EX4650-48Y", "introduced_release": "18.3R1", "source": url}
            for feature, url in [
                ("IPv4 GRE encapsulation", "https://apps.juniper.net/feature-explorer/feature/3185"),
                ("IPv4 GRE de-encapsulation", "https://apps.juniper.net/feature-explorer/feature/3184"),
                ("Static ARP", "https://apps.juniper.net/feature-explorer/feature/2903"),
            ]
        ],
        "interfaces": interfaces, "gre_tunnels": tunnels, "routes": routes, "arp": arp,
        "configuration_tree": _tree(root),
        "limitations": list(dict.fromkeys(local_limitations + limitations)),
    }


def parse_delta_xml(xml: str, parameters: dict[str, str], port_assignments: list[dict],
                    base: dict, port_count: int, limitations: list[str]) -> dict:
    return parse_junos_xml(xml, parameters, port_assignments, base, port_count, limitations,
                          scope="delta_changes_only")


def parse_generated_delta(xml: str, base: dict, port_count: int) -> dict:
    """LLM возвращает XML; параметры и интерфейсы извлекает Python."""
    root = _normalized(_parse(xml))
    parameters = {name: "Именованный параметр Delta XML; требует согласования с базой."
                  for name in sorted(set(PARAMETER.findall(xml)))}
    configured = {node.findtext("name") for node in root.findall("interfaces/interface")}
    ports = [{"port": index, "interface": name, "role": "Интерфейс изменяется Delta XML"}
             for index, name in enumerate(base["available_physical_interfaces"], start=1) if name in configured]
    parsed = parse_delta_xml(xml, parameters, ports, base, port_count, [])
    parsed["port_assignment_status"] = "inferred_from_base_interface_order"
    parsed["delta_is_template"] = parsed["is_template"]
    parsed["base_is_template"] = bool(base.get("is_template"))
    parsed["base_parameters"] = sorted(set(PARAMETER.findall(base["configuration_xml"])))
    parsed["is_template"] = parsed["delta_is_template"] or parsed["base_is_template"]
    return parsed


def parse_base_xml(xml: str, device: dict, port_count: int = 4) -> dict:
    """Метаданные общей базы извлекаются из XML, не запрашиваются у LLM."""
    root = _normalized(_parse(xml))
    parameters = {name: "Именованный параметр XML; значение и адресные связи требуют уточнения."
                  for name in sorted(set(PARAMETER.findall(xml)))}
    configured = {node.findtext("name") for node in root.findall("interfaces/interface")}
    ports = [{"port": index, "interface": name, "role": "Не определена из XML"}
             for index, name in enumerate(device["available_physical_interfaces"], start=1) if name in configured]
    # Типы параметров имеют смысл только после проверки структуры Junos XML.
    parsed = parse_junos_xml(xml, parameters, ports, device, port_count, [], scope="rfc_shared_base")
    parameter_usage: dict[str, list[dict]] = {}

    def collect(node: ET.Element, path: str) -> None:
        value = (node.text or "").strip()
        kind = None
        if path.endswith("/address/name"):
            kind = "ipv4_interface_cidr"
        elif path.endswith("/route/name"):
            kind = "ipv4_network_prefix"
        elif node.tag in {"source", "destination"} or path.endswith("/arp/name"):
            kind = "ipv4_address"
        elif node.tag == "mac":
            kind = "mac_address"
        elif node.tag == "next-hop":
            kind = "ip_or_logical_interface"
        if kind and PARAMETER.fullmatch(value):
            name = PARAMETER.fullmatch(value)[1]
            parameter_usage.setdefault(name, []).append({"path": path, "expected_type": kind})
        # В составном CIDR ${IP}/${PREFIX_LENGTH} IP сохраняет тип адреса без маски.
        elif kind in {"ipv4_interface_cidr", "ipv4_network_prefix"} and "/" in value:
            address, prefix = value.split("/", 1)
            for part, part_kind in ((address, "ipv4_address"), (prefix, "prefix_length")):
                match = PARAMETER.fullmatch(part)
                if match:
                    parameter_usage.setdefault(match[1], []).append({"path": path, "expected_type": part_kind})
        for child in node:
            collect(child, path + "/" + child.tag)

    collect(root, root.tag)
    for name, usages in parameter_usage.items():
        kinds = {item["expected_type"] for item in usages}
        cidr = kinds & {"ipv4_interface_cidr", "ipv4_network_prefix"}
        incompatible = (cidr and kinds & {"ipv4_address", "ip_or_logical_interface"}) or (
            "mac_address" in kinds and len(kinds) > 1) or ("prefix_length" in kinds and len(kinds) > 1)
        if incompatible:
            raise ValueError(f"Параметр ${{{name}}} используется в несовместимых форматах: "
                             + ", ".join(sorted(kinds)) + ". Разделите IP без маски, CIDR и другие значения.")
    parsed["parameter_usage"] = parameter_usage
    for name, usages in parameter_usage.items():
        kinds = sorted({item["expected_type"] for item in usages})
        parsed["parameters"][name] = "Ожидаемый формат: " + ", ".join(kinds) + ". Адресные связи требуют проверки."
    for interface in parsed["interfaces"]:
        if interface["name"] in device["available_physical_interfaces"]:
            for port in parsed["port_assignments"]:
                if port["interface"] == interface["name"] and interface["description"]:
                    port["role"] = interface["description"]
    parsed["port_assignment_status"] = "inferred_from_inventory_order"
    parsed["limitations"].append("Порядок физических соединений условно принят по списку интерфейсов; "
                                 "фактическая коммутация и роли портов не подтверждены.")
    return parsed
