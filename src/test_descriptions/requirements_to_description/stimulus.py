"""Потоки Xinertel и оформление раздела «Тестовое воздействие»."""

import json
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import PROMPTS_DIR, Notify, request_llm
from .junos_xml import PARAMETER
from .preparation import declared_configuration
from .requirements import Requirement


class TrafficFlow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    number: int = Field(ge=1)
    requirement_codes: list[str] = Field(min_length=1)
    port: int = Field(ge=1, le=4)
    dut_interface: str = Field(min_length=1)
    packet_count: int = Field(ge=1)
    fields: list[str] = Field(min_length=1)
    load: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lines(self):
        if any(not text.strip() or "\n" in text or "\r" in text
               for text in [*self.fields, self.load, self.dut_interface]):
            raise ValueError("Поля потока должны быть непустыми однострочными значениями.")
        return self


class UnresolvedRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TrafficParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str | None = None
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def read_previous_format(cls, value):
        # Старые прогоны содержали только определение, без отдельного значения.
        return {"note": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_parameter(self):
        if self.value is not None:
            self.value = self.value.strip()
            if not self.value or "\n" in self.value or "\r" in self.value:
                raise ValueError("Значение параметра должно быть непустой строкой без поясняющего абзаца.")
        self.note = self.note.strip()
        if self.value is None and not self.note:
            raise ValueError("Для неизвестного параметра укажите его назначение в note.")
        return self


class TestStimulus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    flows: list[TrafficFlow]
    parameters: dict[str, TrafficParameter] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    unresolved_requirements: list[UnresolvedRequirement] = Field(default_factory=list)


def _context(base: dict, delta_xml: str, port_count: int) -> dict:
    view = declared_configuration(base["configuration_xml"], delta_xml)
    return {
        "port_mapping": {f"port_{index}": name for index, name in
                         enumerate(base["available_physical_interfaces"], start=1) if index <= port_count},
        "declared_configuration_xml": ET.tostring(view, encoding="unicode"),
        "base_configuration_id": base["id"],
        "note": "Объявленные настройки базы и дельты; применение и состояние DUT не подтверждены.",
    }


def validate_stimulus(stimulus: TestStimulus, requirements: list[Requirement], context: dict) -> None:
    selected = {req.code for req in requirements}
    referenced = {code for flow in stimulus.flows for code in flow.requirement_codes}
    unresolved = {item.code for item in stimulus.unresolved_requirements}
    if referenced | unresolved != selected:
        raise ValueError("Тестовое воздействие: пропущенные АТ: " + ", ".join(sorted(selected - referenced - unresolved))
                         + "; неизвестные АТ: " + ", ".join(sorted((referenced | unresolved) - selected)))
    numbers = [flow.number for flow in stimulus.flows]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("Нумерация потоков должна идти последовательно, начиная с 1.")
    if len(unresolved) != len(stimulus.unresolved_requirements) or any(not item.reason.strip()
                                                                    for item in stimulus.unresolved_requirements):
        raise ValueError("Повтор или пустое объяснение неразработанного воздействия.")
    for flow in stimulus.flows:
        if context["port_mapping"].get(f"port_{flow.port}") != flow.dut_interface:
            raise ValueError(f"Поток {flow.number}: порт Xinertel не соответствует интерфейсу DUT.")
        if len(flow.requirement_codes) != len(set(flow.requirement_codes)):
            raise ValueError(f"Поток {flow.number}: повтор кодов АТ.")
        _validate_gre_fields(flow)
    for requirement in requirements:
        if re.search(r"\bGRE\b", requirement.code + " " + requirement.description.text, re.I) and re.search(
            r"Reserved1", requirement.description.text, re.I
        ) and re.search(
            r"формир", requirement.description.text, re.I
        ) and requirement.code not in unresolved:
            raise ValueError(f"{requirement.code}: не подтверждён способ формирования Reserved1 самим DUT. "
                             "Отметьте недостающее условие в unresolved_requirements; входной поток не доказывает это поведение.")
    configured = set(PARAMETER.findall(context["declared_configuration_xml"]))
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in stimulus.parameters):
        raise ValueError("Новые параметры трафика должны иметь имена A_Z.")
    if any(not note.strip() for note in stimulus.notes):
        raise ValueError("Примечания не должны быть пустыми.")
    if configured & stimulus.parameters.keys():
        raise ValueError("Параметры конфигурации нельзя переопределять как новые параметры трафика.")
    used = set(PARAMETER.findall("\n".join(text for flow in stimulus.flows for text in [*flow.fields, flow.load])))
    if used - configured - stimulus.parameters.keys():
        raise ValueError("Не определены параметры трафика: " + ", ".join(sorted(used - configured - stimulus.parameters.keys())))
    if stimulus.parameters.keys() - used:
        raise ValueError("Объявлены неиспользуемые параметры трафика.")


def _validate_gre_fields(flow: TrafficFlow) -> None:
    """Ловим явные противоречия C/Checksum/Reserved1, не заменяем разбор пакета."""
    text = "; ".join(flow.fields)
    flags = set(re.findall(
        r"\b(?:Checksum\s+Present(?:\s*\(C\))?|C(?:\s*\(Checksum\s+Present\))?)\s*=\s*([01])\b",
        text, flags=re.I,
    ))
    if len(flags) > 1:
        raise ValueError(f"Поток {flow.number}: противоречивые значения GRE Checksum Present.")
    assigned = lambda field: bool(re.search(
        rf"\b{field}\s*=\s*(?!отсутств|нет\b|не\s)\S+", text, re.I))
    absent = lambda field: bool(re.search(rf"\b{field}\s*(?:=\s*)?(?:отсутств|нет\b)", text, re.I))
    if flags == {"0"} and (assigned("Reserved1") or assigned("Checksum") or re.search(
        r"\bReserved1\s+(?:присутствует|присутствующий)", text, re.I
    )):
        raise ValueError(f"Поток {flow.number}: при C=0 поля Checksum и Reserved1 отсутствуют.")
    if flags == {"1"} and (not assigned("Reserved1") or not assigned("Checksum") or
                          absent("Reserved1") or absent("Checksum")):
        raise ValueError(f"Поток {flow.number}: при C=1 задайте поля Checksum и Reserved1.")


def generate_test_stimulus(requirements: list[Requirement], topology: str, base: dict, delta_xml: str,
                           port_count: int, *, notify: Notify | None = None,
                           output: Path | None = None) -> TestStimulus:
    context = _context(base, delta_xml, port_count)
    inputs = {"atomic_requirements": [{"code": req.code, "modifier": req.modifier,
                                       "text": req.description.text} for req in requirements],
              "topology": topology, "configuration": context,
              "delta_xml": delta_xml, "response_schema": TestStimulus.model_json_schema()}
    prompt = (PROMPTS_DIR / "test_stimulus.md").read_text(encoding="utf-8")
    prompt += "\n\nВходные данные:\n" + json.dumps(inputs, ensure_ascii=False, indent=2)
    system = (PROMPTS_DIR / "test_stimulus_system.md").read_text(encoding="utf-8")
    diagnostic = {"prompt": prompt, "system": system, "status": "in_progress"}

    def save() -> None:
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    attempts = []
    diagnostic["attempts"] = attempts
    save()
    try:
        current_prompt = prompt
        for index in range(2):
            attempt = {"prompt": current_prompt}
            attempts.append(attempt)

            def metadata(data: dict) -> None:
                attempt["response_metadata"] = data
                save()

            save()
            raw = request_llm(current_prompt, stage="Исправление тестового воздействия" if index else "Тестовое воздействие",
                              notify=notify, announce=False, system_instructions=system, on_response=metadata)
            diagnostic["response"] = attempt["response"] = raw
            save()
            try:
                result = TestStimulus.model_validate_json(raw)
                validate_stimulus(result, requirements, context)
            except ValueError as error:
                attempt["validation_error"] = str(error)
                save()
                if index:
                    raise
                if notify:
                    notify(f"Ошибка тестового воздействия: {error}\nОдна попытка исправления...", "error")
                current_prompt = (prompt + "\n\n" + (PROMPTS_DIR / "test_stimulus_repair.md").read_text(encoding="utf-8")
                                  + "\n\nПредыдущий ответ:\n" + raw + "\n\nОшибка проверки:\n" + str(error))
            else:
                break
    except (Exception, KeyboardInterrupt) as error:
        diagnostic.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, ValueError):
            diagnostic["error"] = str(error)
        save()
        raise
    diagnostic["status"] = "needs_review" if result.unresolved_requirements else "generated"
    save()
    return result


def render_stimulus(stimulus: TestStimulus, port_count: int) -> str:
    ports = ", ".join(f"port_{index}" for index in range(1, port_count + 1))
    lines = ["1. Применить конфигурацию DUT (см. раздел «Конфигурация устройства»).",
             f"2. Запустить захват трафика на всех портах Xinertel ({ports})."]
    if stimulus.flows:
        lines.append("3. Запустить Поток 1." if len(stimulus.flows) == 1 else
                     f"3. Последовательно запустить потоки 1–{len(stimulus.flows)}.")
    full_flows = []
    for flow in stimulus.flows:
        count = flow.packet_count
        noun = "кадр" if count % 10 == 1 and count % 100 != 11 else (
            "кадра" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "кадров")
        lines.extend(["", f"<ТР {', '.join(flow.requirement_codes)}:", f"Поток {flow.number}",
                      f"{count} {noun} на port_{flow.port} (DUT {flow.dut_interface})."])
        full = [f"{index}. {field.rstrip(';.')};" for index, field in enumerate(flow.fields, start=1)]
        full.append(f"{len(flow.fields) + 1}. Нагрузка: {flow.load.rstrip('.')}.")
        variants = [_flow_difference(reference, flow) for reference in full_flows
                    if (reference.port, reference.dut_interface) == (flow.port, flow.dut_interface)]
        best = min([full, *variants], key=lambda variant: len("\n".join(variant)))
        lines.append("")
        lines.extend(best)
        if best is full:
            full_flows.append(flow)
        lines.extend(["", "ТР>"])
    for item in stimulus.unresolved_requirements:
        lines.extend(["", f"<ТР {item.code}:", f"Воздействие требует уточнения: {item.reason.strip()}", "ТР>"])
    return "\n".join(lines)


def _flow_difference(reference: TrafficFlow, flow: TrafficFlow) -> list[str]:
    # Ссылаемся только на полностью описанный поток; исходные поля в JSON не меняются.
    lines = [f"Поля и нагрузка — как в Потоке {reference.number}; изменения по нумерации его полей:", ""]
    for operation, start, end, other_start, other_end in SequenceMatcher(
        a=reference.fields, b=flow.fields, autojunk=False
    ).get_opcodes():
        if operation == "replace" and end - start == other_end - other_start:
            lines.extend(f"- Поле {index + 1}: {field.rstrip(';.')}."
                         for index, field in zip(range(start, end), flow.fields[other_start:other_end]))
        elif operation != "equal":
            if operation in {"replace", "delete"}:
                lines.extend(f"- Исключить поле {index + 1}." for index in range(start, end))
            if operation in {"replace", "insert"}:
                position = f"после поля {start}" if start else "перед первым полем"
                lines.append(f"- Добавить {position}: " + "; ".join(flow.fields[other_start:other_end]) + ".")
    if flow.load != reference.load:
        lines.append(f"- Нагрузка: {flow.load.rstrip('.')}.")
    if len(lines) == 2:
        return [f"Поля и нагрузка — как в Потоке {reference.number}, без изменений."]
    return lines


def render_stimulus_parameters(stimulus: TestStimulus) -> str | None:
    lines = [f"- ${{{name}}} = {parameter.value if parameter.value is not None else 'не задано'}"
             for name, parameter in stimulus.parameters.items()]
    notes = [f"${{{name}}}: {parameter.note}" for name, parameter in stimulus.parameters.items() if parameter.note]
    notes.extend(note.strip() for note in stimulus.notes)
    if notes:
        if lines:
            lines.append("")
        lines.extend(["Примечания:", ""])
        lines.extend(f"- {note}" for note in dict.fromkeys(notes))
    return "\n".join(lines) if lines else None
