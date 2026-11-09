import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import Notify, request_llm
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


def group_requirements(requirements: list[Requirement], *, notify: Notify | None = None) -> Grouping:
    prompt = """Спланируй набор тестов по ВСЕМ входным атомарным требованиям.
Это этап планирования всего набора тестов, а не описание одного заранее выбранного теста.
Верни только JSON-объект с полем tests — массивом групп.
Каждая группа — объект с полем atomicRequirementCodes: непустым массивом строк
с кодами входных атомарных требований.

Правила:
- Атомарное требование имеет origin = OTHER и непустой parentRequirementCodes.
- Остальные требования даны для понимания контекста; не включай их коды в группы.
- Одно атомарное требование может входить максимум в один тест.
- Один тест может покрывать несколько совместимых атомарных требований.
- Покрой все MUST. Стремись покрыть весь набор; MAY допустимо оставить непокрытыми.
- Не стремись специально минимизировать или максимизировать количество тестов.
- Один тест должен соответствовать одной основной цели и одному переиспользуемому
  сценарию: общей операции DUT, подготовке, типу воздействия и способу проверки.
- Объединяй АТ, если их можно проверить одним сценарием, меняя только параметры
  пакетов или добавляя дополнительные проверки результата.
- Несколько проверок полей одного формируемого или обрабатываемого пакета обычно
  можно объединять в один тест.
- Корректные и некорректные варианты одного механизма обработки могут быть
  параметрами одного теста, если для них не требуется отдельная логика выполнения.
- Создавай отдельный тест, если меняется основная операция DUT, требуется другая
  конфигурация или подготовка, другой самостоятельный тип воздействия либо отдельная
  последовательность действий.
- Общий протокол, стенд, направление RX/TX или похожая конфигурация сами по себе
  не являются достаточной причиной объединять требования.
- Не усложняй тест, построенный вокруг MUST, только ради покрытия MAY. Если MAY
  требует отдельного сценария или настройки, его допустимо оставить непокрытым.
- Сначала выдели основные сценарии, затем распредели между ними АТ.
- После разбиения проверь:
  1) не являются ли две группы параметрическими вариантами одного сценария;
  2) не содержит ли одна группа несколько независимых сценариев.
  При необходимости скорректируй разбиение.
- Пустые группы и выдуманные коды запрещены.
- У каждой группы должно быть только поле atomicRequirementCodes. Не добавляй code.
- Пока не формируй описания, воздействия, конфигурации и критерии прохождения.

Полный набор требований:
""" + json.dumps([req.model_dump() for req in requirements], ensure_ascii=False)
    return Grouping.model_validate_json(request_llm(prompt, stage="Разбиение требований", notify=notify))


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
