"""Короткие команды запуска для полного файла требований."""

import shutil
import subprocess
import sys


REQUIREMENTS = "requirements/GRE_RFC2784.json"
OUTPUT_DIR = "outputs/requirements_to_description"

DOIT_CONFIG = {
    "default_tasks": ["generate"],
    "verbosity": 2,
    "backend": "json",
    "dep_file": ".venv/.doit.json",
}


def _run(no_llm: bool) -> bool:
    folder = "draft" if no_llm else "generated"
    command = [
        sys.executable, "-B", "main.py", REQUIREMENTS,
        "--output", f"{OUTPUT_DIR}/{folder}",
    ]
    if no_llm:
        command.append("--no-llm")
    return subprocess.run(command, check=False).returncode == 0


def task_generate():
    """Разбить весь файл требований и сформировать описания через LLM."""
    return {
        "actions": [(_run, [False])],
        "uptodate": [False],
        "task_dep": ["erase"],
    }


def task_draft():
    """Собрать черновик без обращения к LLM."""
    return {
        "actions": [(_run, [True])],
        "uptodate": [False],
        "task_dep": ["erase"],
    }


def task_erase():
    """Служебная цель для doit clean."""
    return {
        "actions": None,
        "clean": [(shutil.rmtree, ["outputs"], {"ignore_errors": True})],
    }
