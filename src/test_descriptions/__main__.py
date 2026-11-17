import argparse
from pathlib import Path

from dotenv import load_dotenv
from .terminal import interactive_menu, output_directory, run_generation
from .requirements_to_description.junos_xml import DEFAULT_TEST_BASE_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация описаний тестов и Delta XML по требованиям и входной базе.")
    parser.add_argument("requirements_json", nargs="?", type=Path, help="JSON требований; без аргумента — меню")
    parser.add_argument(
        "--output", type=Path, help="Родительский каталог результатов; каждый запуск создаёт отдельный run_…"
    )
    parser.add_argument("--base", type=Path, default=DEFAULT_TEST_BASE_PATH, help="Входная базовая XML-конфигурация")
    parser.add_argument("--resume", action="store_true", help="Продолжить совместимый незавершённый прогон")
    parser.add_argument("--no-llm", action="store_true", help="Собрать черновики без обращения к API")
    args = parser.parse_args()
    load_dotenv()

    if args.requirements_json is None:
        if args.output or args.no_llm or args.resume or args.base != DEFAULT_TEST_BASE_PATH:
            parser.error("Для параметров запуска укажите файл требований.")
        try:
            interactive_menu()
        except (ValueError, KeyboardInterrupt) as error:
            parser.exit(1, f"{error}\n")
        return
    output = args.output or output_directory(args.requirements_json, args.no_llm)
    if not run_generation(args.requirements_json, output, no_llm=args.no_llm, base=args.base, resume_only=args.resume):
        parser.exit(1)


if __name__ == "__main__":
    main()
