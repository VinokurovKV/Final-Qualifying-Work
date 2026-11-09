import json
from pathlib import Path

from pydantic import BaseModel, Field


class Description(BaseModel):
    text: str


class Requirement(BaseModel):
    """Используемые поля исходного JSON; остальные поля не нужны генератору."""

    code: str = Field(min_length=1)
    modifier: str
    origin: str
    description: Description
    tagCodes: list[str] = Field(default_factory=list)
    parentRequirementCodes: list[str] = Field(default_factory=list)

    @property
    def is_atomic(self) -> bool:
        # Рабочее правило исходного набора; сверить при добавлении других наборов.
        return bool(self.parentRequirementCodes) and self.origin == "OTHER"


def load_requirements(path: str | Path) -> list[Requirement]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("JSON требований должен содержать список.")
    requirements = [Requirement.model_validate(item) for item in raw]
    codes = [req.code for req in requirements]
    if len(codes) != len(set(codes)):
        raise ValueError("В JSON повторяются коды требований.")
    return requirements


def select_atomic_requirements(
    requirements: list[Requirement], codes: list[str]
) -> list[Requirement]:
    if not codes:
        raise ValueError("Явно выберите атомарные требования для одного теста.")
    if len(codes) != len(set(codes)):
        raise ValueError("Выбранные коды атомарных требований повторяются.")
    by_code = {req.code: req for req in requirements}
    selected = []
    for code in codes:
        req = by_code.get(code)
        if req is None or not req.is_atomic:
            raise ValueError(f"Не найдено атомарное требование: {code}")
        for parent_code in req.parentRequirementCodes:
            parent = by_code.get(parent_code)
            if parent is None or parent.is_atomic:
                raise ValueError(f"Некорректный родитель {parent_code} у {code}")
        selected.append(req)
    return selected
