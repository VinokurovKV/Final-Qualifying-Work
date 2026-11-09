import argparse
from pathlib import Path

from dotenv import load_dotenv
from .terminal import interactive_menu, output_directory, run_generation


def main() -> None:
    parser = argparse.ArgumentParser(description="Разбиение требований на тесты и формирование описаний.")
    parser.add_argument("requirements_json", nargs="?", type=Path, help="JSON требований; без аргумента — меню")
    parser.add_argument(
        "--output", type=Path, help="Каталог результатов (по умолчанию отдельный для каждого входного файла)"
    )
    parser.add_argument("--no-llm", action="store_true", help="Собрать черновик без API")
    args = parser.parse_args()
    load_dotenv()

    if args.requirements_json is None:
        if args.output or args.no_llm:
            parser.error("Для --output и --no-llm укажите файл требований.")
        try:
            interactive_menu()
        except (ValueError, KeyboardInterrupt) as error:
            parser.exit(1, f"{error}\n")
        return
    output = args.output or output_directory(args.requirements_json, args.no_llm)
    if not run_generation(args.requirements_json, output, no_llm=args.no_llm):
        parser.exit(1)


if __name__ == "__main__":
    main()
