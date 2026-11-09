"""Меню и вывод в терминал. Логика генерации остаётся в pipeline."""

import json
import os
import sys
from pathlib import Path

import questionary
from openai import APIError
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from .requirements_to_description.pipeline import generate_descriptions
from .requirements_to_description.requirements import load_requirements


console = Console()


def output_directory(source: Path, no_llm: bool = False) -> Path:
    return Path("outputs/requirements_to_description") / source.stem / ("draft" if no_llm else "generated")


def run_generation(source: Path, output: Path, *, no_llm: bool = False, resume_only: bool = False) -> bool:
    try:
        with Progress(SpinnerColumn(), TextColumn("{task.description}", markup=False), TimeElapsedColumn(),
                      console=console, transient=True) as progress:
            task = progress.add_task("Подготовка...", total=None)

            def notify(message: str, level: str = "info") -> None:
                if level == "separator":
                    console.print()
                    console.print("=" * 60, style="dim")
                    console.print()
                    return
                style = {"error": "bold red", "success": "green"}.get(level, "cyan")
                console.print(Text(message, style=style))
                progress.reset(task, total=None, description=message)

            generate_descriptions(source, output, no_llm=no_llm, resume_only=resume_only, notify=notify)
        console.print("Готово.", style="bold green")
        return True
    except APIError as error:
        code = getattr(error, "status_code", None) or "соединение"
        console.print(Panel(Text(f"Ошибка сервиса LLM ({code}). Повторите запуск для продолжения."),
                            title="Ошибка LLM", border_style="red", style="bold red"))
    except ValidationError as error:
        details = "\n".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                            for item in error.errors(include_url=False, include_input=False))
        console.print(Text(f"Ошибка формата данных:\n{details}", style="red"))
    except (ValueError, OSError) as error:
        console.print(str(error), style="red", markup=False)
    except KeyboardInterrupt:
        console.print("Остановлено. Готовые этапы сохранены; прогон можно продолжить.", style="yellow")
    return False


def interactive_menu() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Меню требует интерактивного терминала. Используйте doit generate или doit draft.")
    console.print(Panel("Генератор описаний тестов\n[dim]Требования → группы тестов → описания[/dim]",
                        border_style="cyan"))
    while True:
        files = sorted((path for path in Path("requirements").glob("*")
                        if path.is_file() and path.suffix.lower() == ".json"), key=lambda path: path.name.casefold())
        if not files:
            console.print("В requirements/ нет JSON-файлов. Добавьте файл требований и запустите doit снова.", style="yellow")
            return
        source = questionary.select(
            "Выберите файл требований (↑/↓, Enter):",
            choices=[questionary.Choice(path.name, value=path) for path in files]
                    + [questionary.Choice("Выйти", value="exit")],
        ).ask(kbi_msg="Выход.")
        if source is None or source == "exit":
            return
        try:
            requirements = load_requirements(source)
            atomic = [req for req in requirements if req.is_atomic]
            summary = Table.grid(padding=(0, 2))
            summary.add_row("Файл", source.name)
            summary.add_row("Требования", str(len(requirements)))
            summary.add_row("Атомарные требования", str(len(atomic)))
            summary.add_row("Обязательные требования", str(sum(req.modifier.upper() == 'MUST' for req in atomic)))
            summary.add_row("Модель", os.getenv("LLM_MODEL") or "не настроена")
            console.print(summary)
            output = output_directory(source)
            manifest = output / "groups.json"
            unfinished = manifest.exists() and json.loads(manifest.read_text()).get("status") == "in_progress"
        except (ValueError, OSError) as error:
            console.print(str(error), style="red", markup=False)
            continue
        choices = [questionary.Choice("Сформировать описания через LLM", value="generate")]
        if unfinished:
            choices.append(questionary.Choice("Продолжить незавершённый прогон", value="resume"))
        choices.extend([
            questionary.Choice("Собрать черновики без LLM", value="draft"),
            questionary.Choice("Выбрать другой файл", value="back"),
            questionary.Choice("Выйти", value="exit"),
        ])
        action = questionary.select("Что сделать?", choices=choices).ask(kbi_msg="Выход.")
        if action is None or action == "exit":
            return
        if action == "back":
            continue
        no_llm = action == "draft"
        run_generation(source, output_directory(source, no_llm), no_llm=no_llm, resume_only=action == "resume")
