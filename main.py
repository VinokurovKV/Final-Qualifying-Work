"""Запуск из исходников без установки проекта и записи __pycache__."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from test_descriptions.__main__ import main


if __name__ == "__main__":
    main()
