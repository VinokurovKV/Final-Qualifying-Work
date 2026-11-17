from .requirements_to_description.requirements import Requirement


def collect_tags(atomic_requirements: list[Requirement]) -> list[str]:
    return list(dict.fromkeys(tag for req in atomic_requirements for tag in req.tagCodes))


def render_requirements_section(
    all_requirements: list[Requirement],
    atomic_requirements: list[Requirement],
) -> str:
    by_code = {req.code: req for req in all_requirements}
    atomics_by_parent: dict[str, list[Requirement]] = {}

    for atomic in atomic_requirements:
        for parent_code in dict.fromkeys(atomic.parentRequirementCodes):
            atomics_by_parent.setdefault(parent_code, []).append(atomic)

    chunks = ["# Требования", ""]

    for parent_code, atomics in atomics_by_parent.items():
        parent = by_code[parent_code]
        chunks += [f"## {parent.code}", parent.description.text.strip(), ""]

        for atomic in atomics:
            chunks += [
                f"### {atomic.code}",
                atomic.description.text.strip(),
                "",
            ]

    return "\n".join(chunks).rstrip()


def render_topology(port_count: int) -> str:
    ports = "Соединён 1 порт" if port_count == 1 else f"Соединены {port_count} порта"
    return f"# Топология\n\n## Topo2_dev_{port_count}_ports\nXinertel <-> DUT\n{ports}"


def render_readme(
    all_requirements: list[Requirement],
    atomic_requirements: list[Requirement],
    code: str,
    short_description: str,
    port_count: int = 4,
    delta_configuration: str | None = None,
    *,
    base_configuration_id: str | None = None,
    configuration_limitations: list[str] | None = None,
    configuration_is_template: bool = False,
) -> str:
    tags = ", ".join(collect_tags(atomic_requirements))
    requirements_section = render_requirements_section(
        all_requirements,
        atomic_requirements,
    )
    topology = render_topology(port_count)
    configuration = (
        f"## Delta-конфигурация\n\n{delta_configuration}" if delta_configuration is not None else "TODO"
    )
    if base_configuration_id:
        delta = configuration if delta_configuration is not None else "## Delta-конфигурация\n\nTODO"
        configuration = f"## Базовая конфигурация\n\n{base_configuration_id}\n\n{delta}"
    if configuration_is_template:
        configuration += "\n\n**Конфигурация содержит параметры:** значения в базовой конфигурации или Delta XML требуют подстановки; конфигурация не готова к применению."
    if configuration_limitations:
        configuration += "\n\n### Ограничения проверки\n\n" + "\n".join(f"- {item}" for item in configuration_limitations)

    return f"""# Код

{code}

# Краткое описание

{short_description}

# Теги

{tags}

{requirements_section}

{topology}

# Конфигурация устройства

{configuration}

# Тестовое воздействие

TODO

# Критерий прохождения теста

TODO

# Дополнительная информация

TODO
"""
