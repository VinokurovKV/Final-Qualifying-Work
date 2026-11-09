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


def render_readme(
    all_requirements: list[Requirement],
    atomic_requirements: list[Requirement],
    code: str,
    short_description: str,
    port_count: int = 4,
) -> str:
    tags = ", ".join(collect_tags(atomic_requirements))
    requirements_section = render_requirements_section(
        all_requirements,
        atomic_requirements,
    )
    ports_description = "Соединён 1 порт" if port_count == 1 else f"Соединены {port_count} порта"

    return f"""# Код

{code}

# Краткое описание

{short_description}

# Теги

{tags}

{requirements_section}

# Топология

## Topo2_dev_{port_count}_ports
Xinertel <-> DUT
{ports_description}

# Конфигурация устройства

TODO

# Тестовое воздействие

TODO

# Критерий прохождения теста

TODO

# Дополнительная информация

TODO
"""
