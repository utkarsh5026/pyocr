"""The Makefile is a list of aliases for tasks in makefile.py; this keeps the two from drifting apart.

A target missing from either side fails silently in practice (`make foo` says "no rule", or a task exists that
`make help` advertises but `make` can't run), so check it here instead.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("rich")


def _load_makefile_py():
    spec = importlib.util.spec_from_file_location("pyocr_makefile", ROOT / "makefile.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def _phony_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return {name for line in re.findall(r"^\.PHONY:(.*)$", text, re.MULTILINE) for name in line.split()}


def test_makefile_targets_match_tasks():
    tasks = set(_load_makefile_py().TASKS)
    assert _phony_targets() == tasks


def test_every_task_has_a_category_and_help(capsys):
    module = _load_makefile_py()
    for task in module.TASKS.values():
        assert task.category in module.CATEGORIES, task.name
        assert task.help, task.name
    assert module.main(["help"]) == 0
    assert module.main(["no-such-task"]) == 2
