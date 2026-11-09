import argparse
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError

from .requirements_to_description.pipeline import generate_descriptions


def main() -> None:
    parser = argparse.ArgumentParser(description="Разбиение требований на тесты и формирование описаний.")
    parser.add_argument("requirements_json", help="JSON со списком требований")
    parser.add_argument(
        "--output", default="outputs/requirements_to_description/generated", help="Каталог результатов"
    )
    parser.add_argument("--no-llm", action="store_true", help="Собрать черновик без API")
    args = parser.parse_args()
    load_dotenv()

    try:
        generate_descriptions(args.requirements_json, Path(args.output), no_llm=args.no_llm)
    except APIError as error:
        parser.exit(1, f"Ошибка сервиса LLM ({getattr(error, 'status_code', None) or 'соединение'}). "
                    "Повторите запуск: сохранённые этапы будут использованы повторно.\n")


if __name__ == "__main__":
    main()
