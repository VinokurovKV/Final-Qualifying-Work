import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import PROMPTS_DIR, Notify, request_llm
from .requirements import Requirement, select_atomic_requirements


class TestGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    atomicRequirementCodes: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def discard_premature_code(cls, value: object) -> object:
        # Код теста формируется позже; некоторые модели добавляют его уже при группировке.
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "code"}
        return value


class Grouping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tests: list[TestGroup] = Field(min_length=1)


def group_requirements(requirements: list[Requirement], *, notify: Notify | None = None,
                       output: Path | None = None) -> Grouping:
    inputs = {"atomic_requirements": [req.model_dump() for req in requirements if req.is_atomic],
              "context_requirements": [req.model_dump() for req in requirements if not req.is_atomic]}
    prompt = (PROMPTS_DIR / "grouping.md").read_text(encoding="utf-8") + json.dumps(inputs, ensure_ascii=False)
    system = (PROMPTS_DIR / "grouping_system.md").read_text(encoding="utf-8")
    diagnostic = {"prompt": prompt, "system": system}

    def save() -> None:
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
            (output / "grouping.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def metadata(data: dict) -> None:
        diagnostic["response_metadata"] = data
        save()

    save()
    raw = request_llm(prompt, stage="Разбиение требований", notify=notify, system_instructions=system,
                      on_response=metadata)
    diagnostic["response"] = raw
    save()
    return Grouping.model_validate_json(raw)


def validate_grouping(grouping: Grouping, requirements: list[Requirement]) -> dict:
    atomic = [req for req in requirements if req.is_atomic]
    counts = Counter(code for group in grouping.tests for code in group.atomicRequirementCodes)
    repeated = [code for code, count in counts.items() if count > 1]
    if repeated:
        raise ValueError("Повторное покрытие атомарных требований: " + ", ".join(repeated))
    for group in grouping.tests:
        select_atomic_requirements(requirements, group.atomicRequirementCodes)
    must = [req.code for req in atomic if req.modifier.upper() == "MUST"]
    missing_must = [code for code in must if code not in counts]
    if missing_must:
        raise ValueError("Не покрыты MUST: " + ", ".join(missing_must))
    return {
        "atomic_total": len(atomic),
        "atomic_covered": len(counts),
        "must_total": len(must),
        "must_covered": len(must),
        "uncoveredAtomicRequirementCodes": [req.code for req in atomic if req.code not in counts],
    }
