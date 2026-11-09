import hashlib
import json
import os
from pathlib import Path

from ..rendering import render_readme
from .generation import FORMAT_PATH, Notify, TestDescription, generate_test_description
from .grouping import Grouping, TestGroup, group_requirements, validate_grouping
from .requirements import load_requirements, select_atomic_requirements


def generate_descriptions(
    requirements_path: str | Path,
    output: Path,
    *,
    no_llm: bool = False,
    resume_only: bool = False,
    notify: Notify | None = None,
) -> None:
    """Весь файл → группы тестов → проверка покрытия → описания всех групп."""
    def report(message: str, level: str = "info") -> None:
        if notify:
            notify(message, level)
        elif level == "separator":
            print("\n" + "=" * 60 + "\n", flush=True)
        else:
            print(message, flush=True)

    report(f"[1/3] Чтение и проверка требований: {requirements_path}")
    requirements = load_requirements(requirements_path)
    atomic = [req for req in requirements if req.is_atomic]
    if not atomic:
        raise ValueError("Во входном файле нет атомарных требований.")
    report(f"✓ Загружено {len(requirements)} требований, из них {len(atomic)} атомарных.", "success")
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
    if resume_only and not resume:
        raise ValueError("Нет подходящего незавершённого прогона: вход, промпты или модель изменились. "
                         "Выберите генерацию нового набора.")
    report("", "separator")
    if resume:
        grouping = Grouping(tests=[TestGroup(atomicRequirementCodes=test["atomicRequirementCodes"])
                                  for test in manifest["tests"]])
        report("[2/3] Восстановление разбиения незавершённого прогона...")
    else:
        report(f"[2/3] Разбиение {len(atomic)} атомарных требований на тесты...")
        grouping = (
            Grouping(tests=[TestGroup(atomicRequirementCodes=[req.code]) for req in atomic])
            if no_llm else group_requirements(requirements, notify=report)
        )
    report("Проверка разбиения: коды АТ, отсутствие повторов, покрытие MUST...")
    coverage = validate_grouping(grouping, requirements)
    report(f"✓ Разбиение: тестов — {len(grouping.tests)}; "
           f"Атомарные требования {coverage['atomic_covered']}/{coverage['atomic_total']}; "
           f"Обязательные требования {coverage['must_covered']}/{coverage['must_total']}", "success")
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
    report("", "separator")
    test_codes = set()
    for index, (group, test) in enumerate(zip(grouping.tests, manifest["tests"]), start=1):
        selected = select_atomic_requirements(requirements, group.atomicRequirementCodes)
        report(f"[3/3] Описание теста {index}/{len(grouping.tests)}: "
               + ", ".join(group.atomicRequirementCodes))
        if "code" in test:
            description = TestDescription(
                code=test["code"], short_description=test["short_description"], port_count=test.get("port_count", 4)
            )
            report("Уже сохранён, запрос LLM пропущен.")
        else:
            description = (
                TestDescription(code=f"TEST_DRAFT_{index:03d}", short_description="TODO: краткое описание теста.")
                if no_llm else generate_test_description(selected, stage=f"Описание теста {index}/{len(grouping.tests)}", notify=report)
            )
        if description.code in test_codes:
            raise ValueError(f"Повторяется код теста: {description.code}")
        test_codes.add(description.code)
        report(f"Проверка кода теста и сохранение Markdown: {test['file']}")
        (output / test["file"]).write_text(
            render_readme(requirements, selected, description.code, description.short_description, description.port_count),
            encoding="utf-8"
        )
        test.update(description.model_dump())
        _save_manifest(manifest_path, manifest)
        report(f"✓ Описание {index}/{len(grouping.tests)} сохранено: {description.code}", "success")
    manifest["status"] = "complete"
    _save_manifest(manifest_path, manifest)
    report(f"Описания и groups.json сохранены: {output}")


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
