"""Короткие команды запуска. Набор требований текущего теста задаётся здесь."""

import shutil
import subprocess
import sys


REQUIREMENTS = "requirements/GRE_RFC2784.json"
ATOMIC_CODES = [
    "GRE-VERSION-NUMBER.1",
    "GRE-RESERVED0-BITS.1",
    "GRE-PROTOCOL-TYPE.1",
]
OUTPUT_DIR = "outputs/requirements_to_description"

DOIT_CONFIG = {
    "default_tasks": ["generate"],
    "verbosity": 2,
    "backend": "json",
    "dep_file": ".venv/.doit.json",
}


def _run(no_llm: bool) -> bool:
    filename = "draft.md" if no_llm else "gre_encap.md"
    command = [
        sys.executable, "-B", "main.py", REQUIREMENTS,
        "--atomic", *ATOMIC_CODES,
        "--output", f"{OUTPUT_DIR}/{filename}",
    ]
    if no_llm:
        command.append("--no-llm")
    return subprocess.run(command, check=False).returncode == 0


def task_generate():
    """Сформировать описание через LLM (также запускается командой doit)."""
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
        "clean": [(shutil.rmtree, [OUTPUT_DIR], {"ignore_errors": True})],
    }
