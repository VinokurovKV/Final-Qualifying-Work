import hashlib
import json
import os
from pathlib import Path

from ..rendering import render_readme
from .generation import FORMAT_PATH, TestIdentity, generate_test_identity
from .grouping import Grouping, TestGroup, group_requirements, validate_grouping
from .requirements import load_requirements, select_atomic_requirements


def generate_descriptions(
    requirements_path: str | Path,
    output: Path,
    *,
    no_llm: bool = False,
) -> None:
    """Весь файл → группы тестов → проверка покрытия → описания всех групп."""
    requirements = load_requirements(requirements_path)
    atomic = [req for req in requirements if req.is_atomic]
    if not atomic:
        raise ValueError("Во входном файле нет атомарных требований.")
    # Продолжаем только незавершённый прогон с тем же входом, промптами и моделью.
    fingerprint = hashlib.sha256(b"".join([
        Path(requirements_path).read_bytes(), FORMAT_PATH.read_bytes(),
        Path(__file__).with_name("generation.py").read_bytes(),
        Path(__file__).with_name("grouping.py").read_bytes(),
        json.dumps([no_llm, os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL")]).encode(),
    ])).hexdigest()
    manifest_path = output / "groups.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    resume = manifest.get("status") == "in_progress" and manifest.get("fingerprint") == fingerprint
    if resume:
        grouping = Grouping(tests=[TestGroup(atomicRequirementCodes=test["atomicRequirementCodes"])
                                  for test in manifest["tests"]])
        print("Продолжение незавершённого прогона...", flush=True)
    else:
        print(f"Разбиение {len(atomic)} атомарных требований на тесты...", flush=True)
        grouping = (
            Grouping(tests=[TestGroup(atomicRequirementCodes=[req.code]) for req in atomic])
            if no_llm else group_requirements(requirements)
        )
    coverage = validate_grouping(grouping, requirements)
    output.mkdir(parents=True, exist_ok=True)
    if not resume:
        for previous in output.glob("test_*.md"):
            previous.unlink()
        manifest = {
            "source": str(requirements_path), "fingerprint": fingerprint,
            "status": "in_progress",
            "grouping_method": "one_atomic_per_test" if no_llm else "llm",
            "coverage": coverage,
            "tests": [{"atomicRequirementCodes": group.atomicRequirementCodes,
                       "file": f"test_{index:03d}.md"}
                      for index, group in enumerate(grouping.tests, start=1)],
        }
        _save_manifest(manifest_path, manifest)
    test_codes = set()
    for index, (group, test) in enumerate(zip(grouping.tests, manifest["tests"]), start=1):
        selected = select_atomic_requirements(requirements, group.atomicRequirementCodes)
        print(f"Описание теста {index}/{len(grouping.tests)}: "
              + ", ".join(group.atomicRequirementCodes), flush=True)
        if "code" in test:
            identity = TestIdentity(code=test["code"], short_description=test["short_description"])
            print("Уже сохранён, запрос LLM пропущен.", flush=True)
        else:
            identity = (
                TestIdentity(code=f"TEST_DRAFT_{index:03d}", short_description="TODO: краткое описание теста.")
                if no_llm else generate_test_identity(selected)
            )
        if identity.code in test_codes:
            raise ValueError(f"Повторяется код теста: {identity.code}")
        test_codes.add(identity.code)
        (output / test["file"]).write_text(
            render_readme(requirements, selected, identity.code, identity.short_description), encoding="utf-8"
        )
        test.update(identity.model_dump())
        _save_manifest(manifest_path, manifest)
    manifest["status"] = "complete"
    _save_manifest(manifest_path, manifest)
    print(f"Тестов: {len(grouping.tests)}; покрытие АТ: {coverage['atomic_covered']}/{coverage['atomic_total']}; "
          f"MUST: {coverage['must_covered']}/{coverage['must_total']}")
    print(f"Описания и groups.json сохранены: {output}")


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
