"""Общая базовая конфигурация по требованиям и RFC, без готовой входной XML."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from dotenv import load_dotenv
from pypdf import PdfReader

from .generation import PROMPTS_DIR, Notify, request_llm
from .junos_xml import _normalized, _parse, parse_base_xml, wrap_base_configuration
from .requirements import load_requirements
from .addressing import check_addressing


def _save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_standard(source: Path, rfc: Path | None = None) -> Path:
    if rfc is not None:
        if not rfc.is_file():
            raise ValueError(f"Файл RFC не найден: {rfc}")
        return rfc
    expected = re.search(r"RFC[\s_-]*(\d+)", source.stem, re.I)
    matches = [path for path in Path("docs").glob("*.pdf")
               if expected and re.fullmatch(rf"RFC[\s_-]*{expected[1]}", path.stem, re.I)]
    if len(matches) != 1:
        raise ValueError("Не удалось однозначно выбрать RFC из docs/. Укажите PDF через --rfc.")
    return matches[0]


def generate_base_configuration(source: Path, rfc: Path, output: Path, *, notify: Notify | None = None) -> Path:
    def report(message: str, level: str = "info") -> None:
        if notify:
            notify(message, level)
        else:
            print(message, flush=True)

    requirements = load_requirements(source)
    if not any(req.is_atomic for req in requirements):
        raise ValueError("В JSON нет атомарных требований.")
    reader = PdfReader(rfc)
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        raise ValueError("Не удалось извлечь текст RFC из PDF.")
    expected = re.search(r"RFC[\s_-]*(\d+)", source.stem, re.I)
    if not expected or not re.search(rf"RFC\s+{expected[1]}\b", text, re.I):
        raise ValueError("Номер RFC в имени JSON не найден в тексте PDF.")
    standard_id = "RFC" + expected[1]
    device = {
        "id": "BASE_" + standard_id, "device_model": "Juniper EX4650",
        "junos_version": None,
        "available_physical_interfaces": [f"xe-0/0/{number}" for number in range(4)],
        "limitations": [
            "Версия Junos OS и аппаратная подготовка GRE tunnel-services не подтверждены.",
            "Список физических портов — условие эксперимента, фактическая коммутация требует проверки.",
            "Локальный профиль не подтверждает поддержку DUT или полноту подготовки всех тестов RFC.",
        ],
    }
    device["source_sha256"] = hashlib.sha256(json.dumps(device, sort_keys=True).encode()).hexdigest()
    inputs = {
        "requirements": json.loads(source.read_text(encoding="utf-8")),
        "standard": {"id": standard_id, "source": str(rfc), "pages": len(reader.pages),
                     "sha256": hashlib.sha256(rfc.read_bytes()).hexdigest(), "text": text},
        "requirements_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "device": device,
        "topology": {"connection": "Xinertel <-> DUT", "connected_ports": 4},
    }
    for folder in ("configurations", "json", "diagnostics"):
        (output / folder).mkdir(parents=True, exist_ok=True)
    diagnostic_path = output / "diagnostics/base_generation.json"
    system = (PROMPTS_DIR / "rfc_base_system.md").read_text(encoding="utf-8")
    prompt = ((PROMPTS_DIR / "junos_context.md").read_text(encoding="utf-8")
              + "\n\n" + (PROMPTS_DIR / "rfc_base_configuration.md").read_text(encoding="utf-8")
              + "\n\nВходные данные:\n" + json.dumps(inputs, ensure_ascii=False, indent=2))
    # Повтор после сбоя сохраняет предыдущие ответы в том же журнале.
    attempts = json.loads(diagnostic_path.read_text())["attempts"] if diagnostic_path.exists() else []
    diagnostic = {"status": "in_progress", "inputs": inputs, "system": system,
                  "attempts": attempts, "ready_to_apply": False}
    _save(diagnostic_path, diagnostic)

    def ask(text: str, *, repair: bool = False) -> str:
        attempt = {"stage": "repair" if repair else "generation", "prompt": text}
        attempts.append(attempt)
        _save(diagnostic_path, diagnostic)

        def metadata(data: dict) -> None:
            attempt["response_metadata"] = data
            _save(diagnostic_path, diagnostic)

        raw = request_llm(
            text, stage="Исправление базовой конфигурации" if repair else "Базовая конфигурация RFC",
            system_instructions=system, output_format="xml", notify=report, on_response=metadata,
        )
        attempt["response"] = raw
        _save(diagnostic_path, diagnostic)
        return raw

    try:
        raw = ask(prompt)
        for attempt in range(2):
            try:
                parsed = parse_base_xml(raw, device)
                if not parsed["interfaces"]:
                    raise ValueError("База не содержит интерфейсов.")
                parsed["addressing_check"] = check_addressing(_normalized(_parse(raw)), device["available_physical_interfaces"])
            except ValueError as error:
                attempts[-1]["validation_error"] = str(error)
                _save(diagnostic_path, diagnostic)
                if attempt:
                    raise
                report(f"Ошибка локальной проверки: {error}\nОдна попытка исправления...", "error")
                repair = (prompt + "\n\n" + (PROMPTS_DIR / "rfc_base_repair.md").read_text(encoding="utf-8")
                          + "\n\nПредыдущий XML:\n" + raw + "\n\nОшибка проверки:\n" + str(error))
                raw = ask(repair, repair=True)
            else:
                break
        artifact = wrap_base_configuration(raw)
        artifact_path = output / "configurations" / f"{device['id']}.xml"
        artifact_path.write_text(artifact, encoding="utf-8")
        parsed.update(
            requirements_source=str(source), requirements_sha256=inputs["requirements_sha256"],
            standard={key: value for key, value in inputs["standard"].items() if key != "text"},
            artifact=str(artifact_path.relative_to(output)), artifact_format="rpc_sequence",
            artifact_sha256=hashlib.sha256(artifact.encode()).hexdigest(),
            rpc_operations=["edit-config", "get-configuration", "validate", "commit"],
            rpc_executed=False,
        )
        _save(output / "json/base_configuration.json", parsed)
        diagnostic["status"] = "locally_checked"
        _save(diagnostic_path, diagnostic)
    except (Exception, KeyboardInterrupt) as error:
        diagnostic.update(status="failed", error_type=type(error).__name__)
        _save(diagnostic_path, diagnostic)
        raise
    report("✓ Базовая XML прошла ограниченный локальный профиль; RPC сохранены без выполнения.", "success")
    return output


def main() -> None:
    from .pipeline import _create_run_directory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements_json", type=Path)
    parser.add_argument("--rfc", type=Path, required=True, help="Исходный RFC в PDF")
    parser.add_argument("--output", type=Path, help="Родительская папка результатов")
    args = parser.parse_args()
    load_dotenv()
    output = args.output or Path("results/requirements_to_description") / args.requirements_json.stem / "base_configuration"
    output = _create_run_directory(output)
    print(f"Каталог прогона: {output}", flush=True)
    generate_base_configuration(args.requirements_json, args.rfc, output)


if __name__ == "__main__":
    main()
