import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..rendering import render_readme, render_topology
from .configuration import (
    DeviceConfiguration,
    generate_delta_xml,
    generate_device_configuration,
)
from .generation import FORMAT_PATH, PROMPTS_DIR, Notify, TestDescription, generate_test_description
from .grouping import Grouping, TestGroup, group_requirements, validate_grouping
from .requirements import load_requirements, select_atomic_requirements
from .junos_xml import DEFAULT_TEST_BASE_PATH, load_base_configuration, parse_generated_delta
from .preparation import check_preparation


def generate_descriptions(
    requirements_path: str | Path,
    output: Path,
    *,
    no_llm: bool = False,
    resume_only: bool = False,
    notify: Notify | None = None,
    base_configuration_path: Path = DEFAULT_TEST_BASE_PATH,
) -> None:
    """Весь файл → группы тестов → проверка покрытия → описания всех групп."""
    def report(message: str, level: str = "info") -> None:
        if notify:
            notify(message, level)
        elif level == "separator":
            print("\n" + "=" * 60 + "\n", flush=True)
        else:
            print(message, flush=True)

    report(f"[1/4] Чтение и проверка требований: {requirements_path}")
    requirements = load_requirements(requirements_path)
    atomic = [req for req in requirements if req.is_atomic]
    if not atomic:
        raise ValueError("Во входном файле нет атомарных требований.")
    report(f"✓ Загружено {len(requirements)} требований, из них {len(atomic)} атомарных.", "success")
    base = load_base_configuration(base_configuration_path)
    if not base["available_physical_interfaces"]:
        raise ValueError("Во входной базе нет доступных физических интерфейсов.")
    minimum_ports = len(base["available_physical_interfaces"])
    if minimum_ports > 4:
        raise ValueError("Входная база использует больше физических интерфейсов, чем допускает топология прототипа.")
    # Продолжаем только незавершённый прогон с тем же входом, промптами и моделью.
    fingerprint = hashlib.sha256(b"".join([
        Path(requirements_path).read_bytes(), FORMAT_PATH.read_bytes(),
        Path(__file__).read_bytes(),
        Path(__file__).with_name("generation.py").read_bytes(),
        Path(__file__).with_name("grouping.py").read_bytes(),
        Path(__file__).with_name("configuration.py").read_bytes(),
        Path(__file__).with_name("junos_xml.py").read_bytes(),
        Path(__file__).with_name("preparation.py").read_bytes(),
        base_configuration_path.read_bytes() if base is not None else b"",
        (Path(__file__).parents[1] / "rendering.py").read_bytes(),
        *[path.read_bytes() for path in sorted(PROMPTS_DIR.glob("*.md"))],
        json.dumps([no_llm, os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL"), base["id"] if base else None]).encode(),
    ])).hexdigest()
    manifest = {}
    resume = False
    if resume_only:
        candidates = [output / "groups.json", *sorted(output.glob("run_*/groups.json"), reverse=True)]
        for path in candidates:
            if path.exists():
                saved = json.loads(path.read_text())
                if saved.get("status") == "in_progress" and saved.get("fingerprint") == fingerprint:
                    output, manifest, resume = path.parent, saved, True
                    break
        if not resume:
            raise ValueError("Нет совместимого незавершённого прогона: проверьте требования, базу, промпты и модель.")
    else:
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        output = Path(tempfile.mkdtemp(prefix=f"run_{stamp}_", dir=output))
    manifest_path = output / "groups.json"
    report(f"Каталог прогона: {output}")
    if not resume:
        (output / "base_configuration.xml").write_text(base["configuration_xml"], encoding="utf-8")
        (output / "requirements.json").write_bytes(Path(requirements_path).read_bytes())
    report("", "separator")
    if resume:
        grouping = Grouping(tests=[TestGroup(atomicRequirementCodes=test["atomicRequirementCodes"])
                                  for test in manifest["tests"]])
        report("[2/4] Восстановление разбиения незавершённого прогона...")
    else:
        report(f"[2/4] Разбиение {len(atomic)} атомарных требований на тесты...")
        grouping = (
            Grouping(tests=[TestGroup(atomicRequirementCodes=[req.code]) for req in atomic])
            if no_llm else group_requirements(requirements, notify=report, output=output)
        )
    report("Проверка трассировки групп: коды АТ, отсутствие повторов, покрытие MUST...")
    coverage = validate_grouping(grouping, requirements)
    report(f"✓ Разбиение: тестов — {len(grouping.tests)}; "
           f"Атомарные требования {coverage['atomic_covered']}/{coverage['atomic_total']}; "
           f"Обязательные требования {coverage['must_covered']}/{coverage['must_total']}", "success")
    output.mkdir(parents=True, exist_ok=True)
    if not resume:
        manifest = {
            "source": str(requirements_path), "fingerprint": fingerprint,
            "status": "in_progress",
            "grouping_method": "one_atomic_per_test" if no_llm else "llm",
            "configuration_model": None if no_llm else os.getenv("LLM_MODEL"),
            "base_configuration": None if base is None else {
                "id": base["id"], "source": str(base_configuration_path), "source_sha256": base["source_sha256"],
                "ignored_rpc_operations": base["ignored_rpc_operations"],
            },
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
        report(f"[3/4] Описание теста {index}/{len(grouping.tests)}: "
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
        if description.port_count < minimum_ports:
            description.port_count = minimum_ports
            report(f"Число соединений увеличено до {minimum_ports}: столько интерфейсов использует входная база.")
        readme_path = output / test["file"]
        # Не заменяем готовый README промежуточным файлом без ограничений проверки.
        if "device_configuration" not in test or not readme_path.exists():
            report(f"Проверка кода теста и сохранение Markdown: {test['file']}")
            readme_path.write_text(
                render_readme(requirements, selected, description.code, description.short_description, description.port_count,
                              test.get("device_configuration", {}).get("delta_configuration"),
                              base_configuration_id=base["id"] if base else None),
                encoding="utf-8"
            )
        test.update(description.model_dump())
        _save_manifest(manifest_path, manifest)
        report(f"✓ Описание {index}/{len(grouping.tests)} сохранено: {description.code}", "success")
    report("", "separator")
    if no_llm:
        report("[4/4] Конфигурация устройства: пропущена в режиме без LLM.")
    else:
        for index, test in enumerate(manifest["tests"], start=1):
            report(f"[4/4] Конфигурация устройства {index}/{len(grouping.tests)}: {test['code']}")
            selected = select_atomic_requirements(requirements, test["atomicRequirementCodes"])
            topology = render_topology(test["port_count"])
            if "delta_xml" in test:
                xml = test["delta_xml"]
                report("Delta XML уже сохранён, запрос LLM пропущен.")
            else:
                report("Генерация Delta Junos XML...")
                xml = generate_delta_xml(selected, topology, base, notify=report)
            xml_file = Path(test["file"]).with_suffix(".delta.xml").name
            json_file = Path(test["file"]).with_suffix(".configuration.json").name
            report("Локальная проверка XML и преобразование в JSON...")
            for attempt in range(2):
                (output / xml_file).write_text(xml, encoding="utf-8")
                try:
                    parsed = parse_generated_delta(xml, base, test["port_count"])
                    parsed["preparation_check"] = check_preparation(base, xml, selected)
                    parsed["validation"]["local_preparation_checked"] = parsed["preparation_check"]["status"] != "not_applicable"
                except ValueError as error:
                    (output / json_file).write_text(json.dumps({"local_validation_passed": False, "error": str(error)},
                                                              ensure_ascii=False, indent=2), encoding="utf-8")
                    if attempt:
                        raise
                    (output / xml_file.replace(".delta.xml", ".delta.initial.xml")).write_text(xml, encoding="utf-8")
                    report(f"Ошибка Delta XML: {error}\nОдна попытка исправления...", "error")
                    xml = generate_delta_xml(selected, topology, base, notify=report,
                                             previous_xml=xml, validation_error=str(error))
                else:
                    break
            (output / json_file).write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            test["delta_xml"] = xml
            test["configuration_artifacts"] = {"delta_xml": xml_file, "parsed_json": json_file}
            _save_manifest(manifest_path, manifest)
            report("✓ Delta XML разобран локально; проверка поддержки на DUT не выполнялась.", "success")
            if "device_configuration" in test:
                configuration = DeviceConfiguration.model_validate(test["device_configuration"])
                report("Уже сохранена, запрос LLM пропущен.")
            else:
                configuration = generate_device_configuration(
                    selected, topology, parsed, notify=report,
                )
            (output / test["file"]).write_text(render_readme(
                requirements, selected, test["code"], test["short_description"], test["port_count"],
                configuration.delta_configuration,
                base_configuration_id=base["id"], configuration_limitations=parsed["limitations"],
                configuration_is_template=parsed["is_template"],
            ), encoding="utf-8")
            test["device_configuration"] = configuration.model_dump()
            _save_manifest(manifest_path, manifest)
            report(f"✓ Конфигурация {index}/{len(grouping.tests)} сохранена.", "success")
    manifest["status"] = "complete"
    _save_manifest(manifest_path, manifest)
    report(f"Описания и groups.json сохранены: {output}")


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
