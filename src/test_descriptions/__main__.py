import argparse
from pathlib import Path

from dotenv import load_dotenv

from .requirements_to_description.pipeline import generate_description


def main() -> None:
    parser = argparse.ArgumentParser(description="Черновик описания теста по требованиям.")
    parser.add_argument("requirements_json", help="JSON со списком требований")
    parser.add_argument("--atomic", nargs="+", required=True, help="Коды АТ одного теста")
    parser.add_argument(
        "--output", default="outputs/requirements_to_description/generated_readme.md"
    )
    parser.add_argument("--no-llm", action="store_true", help="Собрать черновик без API")
    args = parser.parse_args()
    load_dotenv()

    readme = generate_description(args.requirements_json, args.atomic, no_llm=args.no_llm)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(readme, encoding="utf-8")
    print(f"Черновик сохранён: {output}")


if __name__ == "__main__":
    main()
