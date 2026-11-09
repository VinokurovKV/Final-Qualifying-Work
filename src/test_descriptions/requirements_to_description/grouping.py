import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from .generation import request_llm
from .requirements import Requirement, select_atomic_requirements


class TestGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    atomicRequirementCodes: list[str] = Field(min_length=1)


class Grouping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tests: list[TestGroup] = Field(min_length=1)


def group_requirements(requirements: list[Requirement]) -> Grouping:
    prompt = """Разбей ВЕСЬ входной набор атомарных требований на несколько логически связных тестов.
Это этап планирования всего набора тестов, а не описание одного заранее выбранного теста.
Верни только JSON следующего вида:
{"tests": [{"atomicRequirementCodes": ["REQ-A.1", "REQ-B.1"]}, {"atomicRequirementCodes": ["REQ-C.1"]}]}
Правила:
- Атомарное требование имеет origin = OTHER и непустой parentRequirementCodes.
- Остальные требования даны для понимания контекста; не включай их коды в группы.
- Одно атомарное требование может входить максимум в один тест.
- Один тест может покрывать несколько совместимых атомарных требований.
- Покрой все MUST. Стремись покрыть весь набор; MAY допустимо оставить непокрытыми.
- Не объединяй несвязанные проверки только ради уменьшения числа тестов.
- Не дроби каждое требование в отдельный тест, если проверки совместимы.
- Пустые группы и выдуманные коды запрещены.
- Пока не формируй описания, воздействия, конфигурации и критерии прохождения.

Полный набор требований:
""" + json.dumps([req.model_dump() for req in requirements], ensure_ascii=False)
    return Grouping.model_validate_json(request_llm(prompt))


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
