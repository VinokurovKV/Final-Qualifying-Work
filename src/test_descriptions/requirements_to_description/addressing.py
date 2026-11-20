"""Проверка адресных связей ограниченного IPv4 GRE профиля, без эмуляции Junos."""

import ipaddress
import xml.etree.ElementTree as ET

from .junos_xml import PARAMETER


def check_addressing(view: ET.Element, physical_names: list[str]) -> dict:
    checks = []
    issues = []

    def value(text: str, parser):
        return None if PARAMETER.search(text) else parser(text)

    def record(kind: str, message: str, result: bool | None) -> None:
        checks.append({"kind": kind, "status": "pending_parameters" if result is None else
                       "passed" if result else "failed", "condition": message})
        if result is False:
            issues.append(message)

    units = {}
    addresses = []
    for interface in view.findall("interfaces/interface"):
        if interface.find("disable") is not None:
            continue
        for unit in interface.findall("unit"):
            if unit.find("disable") is not None or unit.find("family/inet") is None:
                continue
            name = f"{interface.findtext('name')}.{unit.findtext('name')}"
            units[name] = interface.findtext("name") in physical_names
            for node in unit.findall("family/inet/address"):
                text = node.findtext("name", "")
                cidr = value(text, ipaddress.IPv4Interface)
                usable = None if cidr is None else (cidr.network.prefixlen >= 31 or
                         cidr.ip not in {cidr.network.network_address, cidr.network.broadcast_address})
                record("interface_address", f"{name}: {text} должен задавать допустимый адрес интерфейса "
                       "с маской, а не адрес сети или broadcast.", usable)
                addresses.append((name, text, cidr))
                for arp in node.findall("arp"):
                    peer_text = arp.findtext("name", "")
                    peer = value(peer_text, ipaddress.IPv4Address)
                    valid = None if peer is None or cidr is None else (
                        peer in cidr.network and peer != cidr.ip and
                        (cidr.network.prefixlen >= 31 or peer not in
                         {cidr.network.network_address, cidr.network.broadcast_address}))
                    record("arp_neighbor", f"{name}: ARP-сосед {peer_text} должен быть доступным адресом "
                           f"в подсети {text} и отличаться от локального адреса DUT.", valid)

    def connected_hop(text: str) -> set[str] | None:
        if text in units:
            # Именованный next-hop локально поддерживается только для GRE point-to-point.
            return {text} if text.startswith("gr-") else set()
        if PARAMETER.search(text):
            return None
        hop = ipaddress.IPv4Address(text)
        if any(cidr is not None and hop == cidr.ip for _, _, cidr in addresses):
            return set()
        found = {name for name, _, cidr in addresses if cidr is not None and hop in cidr.network
                 and (cidr.network.prefixlen >= 31 or hop not in
                      {cidr.network.network_address, cidr.network.broadcast_address})}
        return None if any(cidr is None for _, _, cidr in addresses) else found

    routes = []
    for route in view.findall("routing-options/static/route"):
        prefix = route.findtext("name", "")
        network = value(prefix, ipaddress.IPv4Network)
        hops = []
        for hop in route.findall("next-hop"):
            text = (hop.text or "").strip()
            ports = connected_hop(text)
            hops.append(ports)
            record("static_next_hop", f"Маршрут {prefix}: next-hop {text} должен разрешаться через "
                   "доступный подключённый интерфейс; рекурсивное разрешение этим профилем не подтверждается.",
                   None if ports is None else bool(ports))
        # discard/reject также участвуют в выборе маршрута и не дают путь до endpoint.
        preference = value(route.findtext("preference", "5"), int)
        routes.append((network, preference, hops))

    def endpoint_path(text: str) -> bool | None:
        endpoint = value(text, ipaddress.IPv4Address)
        if endpoint is None:
            return None
        if any(cidr is not None and endpoint == cidr.ip for _, _, cidr in addresses):
            return False
        candidates = []
        unknown = []
        for name, _, cidr in addresses:
            if cidr is None:
                unknown.append(units[name])
            elif endpoint in cidr.network:
                usable = cidr.network.prefixlen >= 31 or endpoint not in {
                    cidr.network.network_address, cidr.network.broadcast_address}
                candidates.append((cidr.network.prefixlen, 0, usable and units[name]))
        for network, preference, hops in routes:
            result = None if any(ports is None for ports in hops) else (
                bool(hops) and all(bool(ports) and all(units[port] for port in ports) for ports in hops))
            if network is None:
                unknown.append(result)
            elif endpoint in network:
                candidates.append((network.prefixlen, preference if preference is not None else 0,
                                   result if preference is not None else None))
        if not candidates:
            return None if any(result is not False for result in unknown) else False
        best_prefix = max(prefix for prefix, _, _ in candidates)
        best_preference = min(pref for prefix, pref, _ in candidates if prefix == best_prefix)
        results = [result for prefix, pref, result in candidates
                   if prefix == best_prefix and pref == best_preference]
        result = None if None in results else all(results)
        if unknown and (result is not False or any(item is not False for item in unknown)):
            return None
        return result

    physical_addresses = [(text, cidr) for name, text, cidr in addresses if units[name]]
    for interface in view.findall("interfaces/interface"):
        if not interface.findtext("name", "").startswith("gr-") or interface.find("disable") is not None:
            continue
        for unit in interface.findall("unit"):
            if unit.find("disable") is not None:
                continue
            name = f"{interface.findtext('name')}.{unit.findtext('name')}"
            source = unit.findtext("tunnel/source", "")
            destination = unit.findtext("tunnel/destination", "")
            if not source or not destination:
                continue  # Наличие полей проверяет check_preparation.
            source_ip = value(source, ipaddress.IPv4Address)
            owned = any(cidr is not None and source_ip == cidr.ip for _, cidr in physical_addresses)
            result = True if owned else None if source_ip is None or any(
                cidr is None for _, cidr in physical_addresses) else False
            record("tunnel_source", f"{name}: source {source} должен совпадать с локальным IPv4 "
                   f"физического интерфейса: {', '.join(text for text, _ in physical_addresses)}.", result)
            record("tunnel_endpoints", f"{name}: source {source} и destination {destination} должны различаться.",
                   False if source == destination else None if PARAMETER.search(source + destination) else True)
            record("underlay_reachability", f"{name}: endpoint {destination} должен иметь путь через физический "
                   "underlay, не через GRE, discard/reject или локальный адрес DUT. При прямом соединении "
                   "endpoint должен принадлежать underlay-подсети; иначе нужен маршрут через достижимый underlay next-hop.",
                   endpoint_path(destination))
    if issues:
        raise ValueError("Нарушены адресные связи IPv4 GRE:\n- " + "\n- ".join(issues))
    return {"status": "pending_parameters" if any(check["status"] == "pending_parameters" for check in checks)
            else "passed", "checks": checks, "scope": "connected_and_static_ipv4_only",
            "device_validation_performed": False}
