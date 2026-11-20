import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..rendering import render_readme, render_topology
from .configuration import (
    DeviceConfiguration,
    generate_delta_xml,
    generate_device_configuration,
)
from .base_configuration import find_standard, generate_base_configuration
from .generation import FORMAT_PATH, PROMPTS_DIR, Notify, TestDescription, generate_test_description
from .grouping import Grouping, TestGroup, group_requirements, validate_grouping
from .requirements import load_requirements, select_atomic_requirements
from .junos_xml import load_base_configuration, parse_base_xml, parse_generated_delta, wrap_base_configuration
from .preparation import check_preparation


def generate_descriptions(
    requirements_path: str | Path,
    output: Path,
    *,
    no_llm: bool = False,
    resume_only: bool = False,
    notify: Notify | None = None,
    base_configuration_path: Path | None = None,
    rfc_path: Path | None = None,
) -> None:
    """Требования и RFC → общая база → группы тестов → описания и дельты."""
    requirements_path = Path(requirements_path)
    def report(message: str, level: str = "info") -> None:
        if notify:
            notify(message, level)
        elif level == "separator":
            print("\n" + "=" * 60 + "\n", flush=True)
        else:
            print(message, flush=True)

    report(f"[1/5] Чтение и проверка требований: {requirements_path}")
    requirements = load_requirements(requirements_path)
    atomic = [req for req in requirements if req.is_atomic]
    if not atomic:
        raise ValueError("Во входном файле нет атомарных требований.")
    report(f"✓ Загружено {len(requirements)} требований, из них {len(atomic)} атомарных.", "success")
    standard = find_standard(requirements_path, rfc_path) if not no_llm and base_configuration_path is None else None
    # Сгенерированная база — результат этапа, а не вход для отпечатка прогона.
    fingerprint = hashlib.sha256(b"".join([
        requirements_path.read_bytes(), FORMAT_PATH.read_bytes(),
        *[Path(__file__).with_name(name).read_bytes() for name in (
            "pipeline.py", "generation.py", "grouping.py", "configuration.py",
            "junos_xml.py", "preparation.py", "base_configuration.py",
        )],
        base_configuration_path.read_bytes() if base_configuration_path is not None and not no_llm else b"",
        standard.read_bytes() if standard is not None else b"",
        (Path(__file__).parents[1] / "rendering.py").read_bytes(),
        *[path.read_bytes() for path in sorted(PROMPTS_DIR.iterdir()) if path.suffix in {".md", ".xml"}],
        json.dumps([no_llm, os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL"),
                    base_configuration_path.stem if base_configuration_path is not None and not no_llm else None]).encode(),
    ])).hexdigest()
    manifest = {}
    resume = False
    if resume_only:
        candidates = sorted(
            (path for path in [output / "json/groups.json", *output.glob("run_*/json/groups.json")] if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns, reverse=True,
        )
        for path in candidates:
            saved = json.loads(path.read_text())
            if saved.get("status") == "in_progress" and saved.get("fingerprint") == fingerprint:
                output, manifest, resume = path.parent.parent, saved, True
                break
        if not resume:
            raise ValueError("Нет совместимого незавершённого прогона: проверьте требования, RFC, промпты и модель.")
    else:
        output = _create_run_directory(output)
    (output / "json").mkdir(exist_ok=True)
    manifest_path = output / "json/groups.json"
    report(f"Каталог прогона: {output}")
    if not resume:
        (output / "json/requirements.json").write_bytes(requirements_path.read_bytes())
        manifest = {
            "source": str(requirements_path), "fingerprint": fingerprint, "status": "in_progress",
            "grouping_method": "one_atomic_per_test" if no_llm else "llm",
            "configuration_model": None if no_llm else os.getenv("LLM_MODEL"),
            "base_configuration": None, "tests": [],
        }
        _save_manifest(manifest_path, manifest)
    report("", "separator")
    base = None
    minimum_ports = 1
    if no_llm:
        report("[2/5] Общая базовая конфигурация: пропущена в режиме без LLM.")
    else:
        report("[2/5] Общая базовая конфигурация RFC...")
        if base_configuration_path is None:
            diagnostic_path = output / "diagnostics/base_generation.json"
            if diagnostic_path.exists() and json.loads(diagnostic_path.read_text()).get("status") == "locally_checked":
                report("База уже сохранена, запрос LLM пропущен.")
            else:
                generate_base_configuration(requirements_path, standard, output, notify=report)
            saved_base = json.loads((output / "json/base_configuration.json").read_text())
            base_configuration_path = output / saved_base["artifact"]
            if hashlib.sha256(base_configuration_path.read_bytes()).hexdigest() != saved_base["artifact_sha256"]:
                raise ValueError("Сохранённая базовая XML изменена после локальной проверки.")
            device = json.loads(diagnostic_path.read_text())["inputs"]["device"]
            base = load_base_configuration(base_configuration_path)
            parse_base_xml(base["configuration_xml"], device)
        else:
            base = load_base_configuration(base_configuration_path)
            report("Используется явно заданная входная база.")
        minimum_ports = len(base["available_physical_interfaces"])
        if not minimum_ports or minimum_ports > 4:
            raise ValueError("База должна использовать от одного до четырёх доступных физических интерфейсов.")
        manifest["base_configuration"] = {
            "id": base["id"], "source": str(base_configuration_path), "source_sha256": base["source_sha256"],
            "ignored_rpc_operations": base["ignored_rpc_operations"],
        }
        if standard is None:
            (output / "configurations").mkdir(exist_ok=True)
            artifact = output / "configurations" / f"{base['id']}.xml"
            artifact.write_text(wrap_base_configuration(base["configuration_xml"]), encoding="utf-8")
        manifest["base_configuration"]["file"] = f"configurations/{base['id']}.xml"
        _save_manifest(manifest_path, manifest)
    report("", "separator")
    if manifest["tests"]:
        grouping = Grouping(tests=[TestGroup(atomicRequirementCodes=test["atomicRequirementCodes"])
                                  for test in manifest["tests"]])
        report("[3/5] Восстановление разбиения незавершённого прогона...")
    else:
        report(f"[3/5] Разбиение {len(atomic)} атомарных требований на тесты...")
        grouping = (
            Grouping(tests=[TestGroup(atomicRequirementCodes=[req.code]) for req in atomic])
            if no_llm else group_requirements(requirements, notify=report, output=output / "diagnostics")
        )
    report("Проверка трассировки групп: коды АТ, отсутствие повторов, покрытие MUST...")
    coverage = validate_grouping(grouping, requirements)
    report(f"✓ Разбиение: тестов — {len(grouping.tests)}; "
           f"Атомарные требования {coverage['atomic_covered']}/{coverage['atomic_total']}; "
           f"Обязательные требования {coverage['must_covered']}/{coverage['must_total']}", "success")
    manifest["coverage"] = coverage
    if not manifest["tests"]:
        manifest["tests"] = [{"atomicRequirementCodes": group.atomicRequirementCodes,
                              "file": f"test_{index:03d}.md"}
                             for index, group in enumerate(grouping.tests, start=1)]
    _save_manifest(manifest_path, manifest)
    report("", "separator")
    test_codes = set()
    for index, (group, test) in enumerate(zip(grouping.tests, manifest["tests"]), start=1):
        selected = select_atomic_requirements(requirements, group.atomicRequirementCodes)
        report(f"[4/5] Описание теста {index}/{len(grouping.tests)}: "
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
        # Не заменяем готовый текст конфигурации промежуточным TODO.
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
        report("[5/5] Конфигурация устройства: пропущена в режиме без LLM.")
    else:
        (output / "diagnostics").mkdir(exist_ok=True)
        _save_validation_report(output, manifest)
        for index, test in enumerate(manifest["tests"], start=1):
            report(f"[5/5] Конфигурация устройства {index}/{len(grouping.tests)}: {test['code']}")
            selected = select_atomic_requirements(requirements, test["atomicRequirementCodes"])
            topology = render_topology(test["port_count"])
            if "delta_xml" in test:
                xml = test["delta_xml"]
                report("Delta XML уже сохранён, запрос LLM пропущен.")
            else:
                report("Генерация Delta Junos XML...")
                xml = generate_delta_xml(selected, topology, base, notify=report)
            xml_file = "configurations/" + Path(test["file"]).with_suffix(".delta.xml").name
            json_file = "json/" + Path(test["file"]).with_suffix(".configuration.json").name
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
                    _save_validation_report(output, manifest)
                    if attempt:
                        raise
                    (output / "diagnostics" / Path(xml_file).name.replace(".delta.xml", ".delta.initial.xml")).write_text(xml, encoding="utf-8")
                    report(f"Ошибка Delta XML: {error}\nОдна попытка исправления...", "error")
                    xml = generate_delta_xml(selected, topology, base, notify=report,
                                             previous_xml=xml, validation_error=str(error))
                else:
                    break
            (output / json_file).write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            test["delta_xml"] = xml
            test["configuration_artifacts"] = {"delta_xml": xml_file, "parsed_json": json_file}
            _save_manifest(manifest_path, manifest)
            _save_validation_report(output, manifest)
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
                base_configuration_id=base["id"],
            ), encoding="utf-8")
            test["device_configuration"] = configuration.model_dump()
            _save_manifest(manifest_path, manifest)
            _save_validation_report(output, manifest)
            report(f"✓ Конфигурация {index}/{len(grouping.tests)} сохранена.", "success")
    manifest["status"] = "complete"
    _save_manifest(manifest_path, manifest)
    if not no_llm:
        _save_validation_report(output, manifest)
    report(f"Описания сохранены: {output}")


def _create_run_directory(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    name = datetime.now(ZoneInfo("Europe/Moscow")).strftime("run_%Y:%m:%dT%H:%M:%S")
    number = 1
    while True:
        path = parent / (name if number == 1 else f"{name}_{number}")
        try:
            path.mkdir()
        except FileExistsError:
            number += 1
        else:
            return path


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _save_validation_report(output: Path, manifest: dict) -> None:
    """Служебная диагностика хранится отдельно от описаний тестов."""
    lines = ["# Локальная проверка конфигураций", "",
             f"Базовая конфигурация: {manifest['base_configuration']['id']}.", "",
             "Проверяются XML и необходимые условия подготовки по объявленным базе и дельте. "
             "Поддержка DUT, прохождение АТ и достаточность конкретного сценария не подтверждены.", ""]
    for test in manifest["tests"]:
        lines.extend([f"## {test.get('code', test['file'])}", ""])
        artifact = output / "json" / Path(test["file"]).with_suffix(".configuration.json")
        if not artifact.exists():
            lines.extend(["Конфигурация ещё не проверена.", ""])
            continue
        data = json.loads(artifact.read_text(encoding="utf-8"))
        if data.get("local_validation_passed") is False:
            lines.extend(["Отклонено локальной проверкой:", "", "```text", data["error"], "```", ""])
            continue
        lines.extend(["Локальная проверка XML пройдена.", "",
                      "Текст конфигурации: " + ("сформирован." if "device_configuration" in test else "ожидается."), ""])
        preparation = data.get("preparation_check", {})
        if preparation.get("status") == "passed_necessary_conditions":
            lines.extend(["Необходимые условия профиля IPv4 GRE проверены по базе и дельте.", ""])
        else:
            lines.extend(["Проверка необходимых условий IPv4 GRE к этим АТ не применялась.", ""])
        if data.get("is_template"):
            lines.extend(["База или дельта содержит именованные параметры; значения и адресные связи требуют проверки.", ""])
        limitations = data.get("limitations", [])
        if limitations:
            lines.extend(["Ограничения:", "", *[f"- {item}" for item in limitations], ""])
        lines.extend([f"Подробности: [{artifact.name}](../json/{artifact.name}).", ""])
    (output / "diagnostics").mkdir(exist_ok=True)
    (output / "diagnostics/validation_report.md").write_text("\n".join(lines), encoding="utf-8")
