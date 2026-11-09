from pathlib import Path

from ..rendering import render_readme
from .generation import TestIdentity, generate_test_identity
from .requirements import load_requirements, select_atomic_requirements


def generate_description(
    requirements_path: str | Path,
    atomic_codes: list[str],
    *,
    no_llm: bool = False,
) -> str:
    """Загрузить → выбрать требования → получить название → собрать Markdown."""
    requirements = load_requirements(requirements_path)
    selected = select_atomic_requirements(requirements, atomic_codes)
    identity = (
        TestIdentity(code="TEST_DRAFT", short_description="TODO: краткое описание теста.")
        if no_llm
        else generate_test_identity(selected)
    )
    return render_readme(requirements, selected, identity.code, identity.short_description)
