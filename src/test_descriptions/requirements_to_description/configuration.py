import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .generation import PROMPTS_DIR, Notify, request_llm
from .requirements import Requirement


CONFIGURATION_PROMPT_PATH = PROMPTS_DIR / "device_configuration.md"


class DeviceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    delta_configuration: str = Field(min_length=1)

    @field_validator("delta_configuration")
    @classmethod
    def validate_body(cls, value: str) -> str:
        value = value.strip()
        if value.splitlines()[:1] == ["## Delta-конфигурация"]:
            value = value.partition("\n")[2].strip()
        if not value:
            raise ValueError("Delta-конфигурация не должна быть пустой.")
        if re.search(r"^\s*#{1,2}\s", value, flags=re.MULTILINE):
            raise ValueError("Верните только текст Delta-конфигурации без заголовков README.")
        return value


def _requirements_data(requirements: list[Requirement]) -> list[dict]:
    return [
        {"code": req.code, "modifier": req.modifier, "text": req.description.text}
        for req in requirements
    ]


def generate_delta_xml(requirements: list[Requirement], topology: str, base: dict,
                       *, notify: Notify | None = None, previous_xml: str | None = None,
                       validation_error: str | None = None) -> str:
    prompt = (PROMPTS_DIR / "device_delta_xml.md").read_text(encoding="utf-8")
    prompt += "\n\n" + (PROMPTS_DIR / "junos_context.md").read_text(encoding="utf-8")
    prompt += "\n\nВходные данные:\n" + json.dumps({
        "atomic_requirements": _requirements_data(requirements),
        "topology": topology, "base_configuration": base,
    }, ensure_ascii=False, indent=2)
    if previous_xml is not None:
        prompt += "\n\n" + (PROMPTS_DIR / "device_delta_repair.md").read_text(encoding="utf-8")
        prompt += "\n\nПредыдущий Delta XML:\n" + previous_xml + "\n\nОшибка проверки:\n" + (validation_error or "")
    return request_llm(
        prompt, stage="Исправление Delta XML" if previous_xml is not None else "Delta Junos XML", notify=notify, announce=False,
        output_format="xml",
        system_instructions=(PROMPTS_DIR / "junos_xml_system.md").read_text(encoding="utf-8"),
    )


def generate_device_configuration(
    requirements: list[Requirement], topology: str, parsed_configuration: dict,
    *, notify: Notify | None = None,
) -> DeviceConfiguration:
    prompt = CONFIGURATION_PROMPT_PATH.read_text(encoding="utf-8") + "\n\nВходные данные:\n" + json.dumps({
        "atomic_requirements": _requirements_data(requirements),
        "topology": topology,
        "parsed_configuration": parsed_configuration,
    }, ensure_ascii=False, indent=2)
    return DeviceConfiguration.model_validate_json(request_llm(
        prompt, stage="Конфигурация устройства", notify=notify, announce=False,
        system_instructions=(PROMPTS_DIR / "device_configuration_system.md").read_text(encoding="utf-8"),
    ))
