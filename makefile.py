#!/usr/bin/env python3
"""Developer task runner for pyocr-toolkit, rendered with rich.

Why a Python file behind the Makefile instead of shell recipes: the project runs on Linux, macOS *and* Windows
(see CI), and shell one-liners in a Makefile don't survive cmd.exe. Every `make <target>` is a thin alias for
`python makefile.py <target>`, so Windows users without `make` get the same commands, and the logic (which
interpreter, which paths, what counts as "done") lives in one readable place.

Each task is a function registered with `@task(...)`, which gives it a category, an emoji and a one-line help
string. `python makefile.py` (or `make`) with no task prints the grouped help table built from that registry.

Extra arguments are passed straight through to the underlying tool:

    python makefile.py test -k compare          make test ARGS="-k compare"
    python makefile.py serve --port 9000        make serve ARGS="--port 9000"
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
except ImportError:  # `make install` works without rich; everything else needs it.
    print("makefile.py needs 'rich'. Run `make install` (or `pip install -e \".[dev]\"`) first.", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
PY = sys.executable
PKG_INIT = ROOT / "src" / "pyocr_toolkit" / "__init__.py"

# Windows consoles may not be UTF-8: degrade emojis instead of crashing (same trick as the pyocr CLI).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

console = Console(highlight=False)


# ---------------------------------------------------------------- registry
@dataclass(frozen=True)
class Category:
    title: str
    emoji: str
    style: str


SETUP = Category("Setup", "🛠️ ", "cyan")
QUALITY = Category("Quality", "🧪", "green")
RUN = Category("Run", "🚀", "magenta")
BUILD = Category("Build & Release", "📦", "yellow")
DOCKER = Category("Docker", "🐳", "blue")
MISC = Category("Info & Housekeeping", "🧰", "bright_black")
CATEGORIES = [SETUP, QUALITY, RUN, BUILD, DOCKER, MISC]


@dataclass(frozen=True)
class Task:
    name: str
    category: Category
    emoji: str
    help: str
    func: Callable[[list[str]], int]
    example: str = ""


TASKS: dict[str, Task] = {}


def task(name: str, category: Category, emoji: str, help: str, example: str = ""):
    """Register a function as `python makefile.py <name>`. The function gets the extra CLI args, returns an exit code."""

    def decorator(func: Callable[[list[str]], int]) -> Callable[[list[str]], int]:
        TASKS[name] = Task(name, category, emoji, help, func, example)
        return func

    return decorator


# ---------------------------------------------------------------- helpers
def version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', PKG_INIT.read_text(encoding="utf-8"))
    return match.group(1) if match else "?"


def fmt_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {seconds % 60:.0f}s"


def run(cmd: list[str], title: str, emoji: str = "▶️ ", env: dict[str, str] | None = None) -> int:
    """Run a command with a header rule and a ✅/❌ footer. Output streams live, it is never captured."""
    console.print()
    console.print(Rule(f"{emoji} [bold]{title}[/]", style="bright_black", align="left"))
    shown = " ".join("python" if part == PY else part for part in cmd)
    console.print(f"[dim]❯ {shown}[/]\n")
    start = time.perf_counter()
    try:
        code = subprocess.call(cmd, cwd=ROOT, env={**os.environ, **(env or {})})
    except FileNotFoundError:
        console.print(f"[red]❌ {cmd[0]} not found on PATH.[/]")
        return 127
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹️  interrupted[/]")
        return 130
    elapsed = fmt_seconds(time.perf_counter() - start)
    if code == 0:
        console.print(f"\n[green]✅ {title} passed[/] [dim]in {elapsed}[/]")
    else:
        console.print(f"\n[red]❌ {title} failed (exit {code})[/] [dim]after {elapsed}[/]")
    return code


def need(binary: str, hint: str) -> bool:
    """Return True if `binary` is on PATH, otherwise print a friendly install hint."""
    if shutil.which(binary):
        return True
    console.print(Panel(f"[bold]{binary}[/] isn't installed or isn't on PATH.\n{hint}",
                        title="❌ missing tool", border_style="red", expand=False))
    return False


def need_module(module: str, install: str) -> bool:
    import importlib.util

    if importlib.util.find_spec(module):
        return True
    console.print(Panel(f"Python package [bold]{module}[/] is missing.\nInstall it with: [cyan]{install}[/]",
                        title="❌ missing package", border_style="red", expand=False))
    return False


def tesseract_version() -> str | None:
    exe = shutil.which("tesseract")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return "installed (version unknown)"


# ---------------------------------------------------------------- 🛠️ setup
@task("install", SETUP, "📥", "Install the package in editable mode with dev tools")
def t_install(args: list[str]) -> int:
    return run([PY, "-m", "pip", "install", "-e", ".[dev]", *args], "pip install -e .[dev]", "📥")


@task("init", SETUP, "📝", "Write a .env template to the repo root (pyocr init)", "make init ARGS=--force")
def t_init(args: list[str]) -> int:
    return run([PY, "-m", "pyocr_toolkit", "init", *args], "pyocr init", "📝")


@task("doctor", SETUP, "🩺", "Check Tesseract, LLM providers and the data directory")
def t_doctor(args: list[str]) -> int:
    return run([PY, "-m", "pyocr_toolkit", "doctor", *args], "pyocr doctor", "🩺")


@task("providers", SETUP, "🤖", "List LLM providers and whether each one is configured")
def t_providers(args: list[str]) -> int:
    return run([PY, "-m", "pyocr_toolkit", "providers", *args], "pyocr providers", "🤖")


# ---------------------------------------------------------------- 🧪 quality
@task("lint", QUALITY, "🔍", "Lint with ruff (same command as CI)")
def t_lint(args: list[str]) -> int:
    return run([PY, "-m", "ruff", "check", "src", "tests", *args], "ruff check", "🔍")


@task("fix", QUALITY, "🔧", "Auto-fix safe lint issues (import order, upgrades, ...)")
def t_fix(args: list[str]) -> int:
    return run([PY, "-m", "ruff", "check", "--fix", "src", "tests", *args], "ruff check --fix", "🔧")


@task("test", QUALITY, "🧪", "Run the test suite (no network or API keys needed)", 'make test ARGS="-k compare"')
def t_test(args: list[str]) -> int:
    code = run([PY, "-m", "pytest", "-q", *args], "pytest", "🧪")
    if not shutil.which("tesseract"):
        console.print("[yellow]⚠️  tesseract isn't on PATH, so the OCR tests were skipped.[/]")
    return code


@task("check", QUALITY, "✅", "Lint + tests with a summary: the project's definition of done")
def t_check(args: list[str]) -> int:
    steps = [("🔍", "Lint", lambda: t_lint([])), ("🧪", "Tests", lambda: t_test(args))]
    results: list[tuple[str, str, int, float]] = []
    for emoji, label, step in steps:
        start = time.perf_counter()
        code = step()
        results.append((emoji, label, code, time.perf_counter() - start))

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
    table.add_column("Step")
    table.add_column("Result")
    table.add_column("Time", justify="right", style="dim")
    for emoji, label, code, seconds in results:
        status = "[green]✅ passed[/]" if code == 0 else f"[red]❌ failed (exit {code})[/]"
        table.add_row(f"{emoji} {label}", status, fmt_seconds(seconds))
    ocr = "[green]✅ ran[/]" if shutil.which("tesseract") else "[yellow]⚠️  skipped (no tesseract)[/]"
    table.add_row("🔤 OCR tests", ocr, "")

    ok = all(code == 0 for *_, code, _ in results)
    console.print()
    console.print(Panel(table, title="📋 check summary", border_style="green" if ok else "red", expand=False))
    return 0 if ok else 1


# ---------------------------------------------------------------- 🚀 run
@task("serve", RUN, "🌐", "Start the web app on http://127.0.0.1:8000", 'make serve ARGS="--port 9000 --open"')
def t_serve(args: list[str]) -> int:
    return run([PY, "-m", "pyocr_toolkit", "serve", *args], "pyocr serve", "🌐")


@task("dev", RUN, "♻️ ", "Start the app with auto-reload on source changes (127.0.0.1:8000)")
def t_dev(args: list[str]) -> int:
    cmd = [PY, "-m", "uvicorn", "pyocr_toolkit.api:create_app", "--factory", "--reload",
           "--reload-dir", "src", "--host", "127.0.0.1", "--port", "8000", *args]
    return run(cmd, "uvicorn --reload", "♻️ ")


@task("site", RUN, "📚", "Preview the GitHub Pages site in docs/ on http://127.0.0.1:8001")
def t_site(args: list[str]) -> int:
    port = args[0] if args else "8001"
    console.print(f"[cyan]📚 Serving docs/ at[/] [bold link=http://127.0.0.1:{port}]http://127.0.0.1:{port}[/]")
    return run([PY, "-m", "http.server", port, "--bind", "127.0.0.1", "--directory", "docs"], "docs site", "📚")


# ---------------------------------------------------------------- 📦 build & release
@task("build", BUILD, "🏗️ ", "Build the wheel and sdist into dist/")
def t_build(args: list[str]) -> int:
    if not need_module("build", "pip install build"):
        return 1
    code = run([PY, "-m", "build", *args], "python -m build", "🏗️ ")
    dist = ROOT / "dist"
    if code == 0 and dist.is_dir():
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("📦 Artifact")
        table.add_column("Size", justify="right")
        for f in sorted(dist.iterdir()):
            table.add_row(f.name, f"{f.stat().st_size / 1024:.0f} KB")
        console.print(table)
    return code


@task("version", BUILD, "🏷️ ", "Show the version and check __init__.py and pyproject.toml agree")
def t_version(args: list[str]) -> int:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    in_toml = match.group(1) if match else "?"
    in_init = version()
    ok = in_toml == in_init

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Version", style="bold")
    table.add_row("src/pyocr_toolkit/__init__.py", in_init)
    table.add_row("pyproject.toml", in_toml)
    console.print(table)
    if ok:
        console.print(f"[green]✅ in sync: {in_init}[/]")
        return 0
    console.print("[red]❌ out of sync. Set the same version in both files (and add a CHANGELOG.md entry).[/]")
    return 1


# ---------------------------------------------------------------- 🐳 docker
DOCKER_HINT = "Install Docker Desktop or Docker Engine: https://docs.docker.com/get-docker/"


@task("docker-build", DOCKER, "🔨", "Build the pyocr-toolkit image (Tesseract included)")
def t_docker_build(args: list[str]) -> int:
    if not need("docker", DOCKER_HINT):
        return 1
    return run(["docker", "build", "-t", "pyocr-toolkit", *args, "."], "docker build", "🔨")


@task("docker-up", DOCKER, "⬆️ ", "Start the app with docker compose on 127.0.0.1:8000")
def t_docker_up(args: list[str]) -> int:
    if not need("docker", DOCKER_HINT):
        return 1
    return run(["docker", "compose", "up", "-d", "--build", *args], "docker compose up", "⬆️ ")


@task("docker-down", DOCKER, "⬇️ ", "Stop the compose stack (the data volume is kept)")
def t_docker_down(args: list[str]) -> int:
    if not need("docker", DOCKER_HINT):
        return 1
    return run(["docker", "compose", "down", *args], "docker compose down", "⬇️ ")


@task("docker-logs", DOCKER, "📜", "Follow the container logs")
def t_docker_logs(args: list[str]) -> int:
    if not need("docker", DOCKER_HINT):
        return 1
    return run(["docker", "compose", "logs", "-f", *args], "docker compose logs", "📜")


# ---------------------------------------------------------------- 🧰 info & housekeeping
@task("info", MISC, "ℹ️ ", "Show the environment: Python, package, Tesseract, Docker, git")
def t_info(args: list[str]) -> int:
    def found(value: str | None, missing: str = "not found") -> str:
        return f"[green]{value}[/]" if value else f"[yellow]{missing}[/]"

    branch = None
    if shutil.which("git"):
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        branch = out.stdout.strip() or None
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("📦 pyocr-toolkit", version())
    table.add_row("🐍 Python", f"{platform.python_version()} [dim]({PY})[/]")
    table.add_row("🫧 Virtualenv", "[green]active[/]" if in_venv else "[yellow]none (system Python)[/]")
    table.add_row("💻 Platform", f"{platform.system()} {platform.machine()}")
    table.add_row("🔤 Tesseract", found(tesseract_version(), "not found: OCR disabled, OCR tests skip"))
    table.add_row("🐳 Docker", found(shutil.which("docker")))
    table.add_row("🌿 Git branch", found(branch))
    console.print(Panel(table, title="ℹ️  environment", border_style="cyan", expand=False))
    return 0


CLEAN_TOP = ["build", "dist", ".pytest_cache", ".ruff_cache", "htmlcov", ".coverage"]
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "data"}


@task("clean", MISC, "🧹", "Delete build output and caches (never touches data/ or .env)")
def t_clean(args: list[str]) -> int:
    targets = [ROOT / name for name in CLEAN_TOP]
    targets += list(ROOT.glob("*.egg-info")) + list((ROOT / "src").glob("*.egg-info"))
    for dirpath, dirnames, _ in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "__pycache__" in dirnames:
            targets.append(Path(dirpath) / "__pycache__")
            dirnames.remove("__pycache__")

    removed = []
    for path in targets:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed.append(path.relative_to(ROOT).as_posix())

    if not removed:
        console.print("[green]✨ Already clean.[/]")
        return 0
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column(f"🧹 Removed {len(removed)} item(s)")
    for item in sorted(removed):
        table.add_row(f"[dim]{item}[/]")
    console.print(table)
    return 0


@task("help", MISC, "❓", "Show this help")
def t_help(args: list[str]) -> int:
    header = Text.assemble(("📄 pyocr-toolkit ", "bold cyan"), (f"v{version()}", "bold"),
                           ("  ·  developer commands", "dim"))
    console.print(Panel(header, border_style="cyan", expand=False))

    for cat in CATEGORIES:
        tasks = [t for t in TASKS.values() if t.category is cat]
        table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 1), pad_edge=False)
        table.add_column(no_wrap=True, width=3)
        table.add_column(style=f"bold {cat.style}", no_wrap=True, min_width=20)
        table.add_column()
        for t in tasks:
            table.add_row(t.emoji, f"make {t.name}", t.help)
        console.print(Panel(table, title=f"{cat.emoji} [bold {cat.style}]{cat.title}[/]", title_align="left",
                            border_style=cat.style, expand=False, padding=(0, 1)))

    examples = Table(box=None, show_header=False, padding=(0, 1))
    examples.add_column(style="green", no_wrap=True)
    for t in TASKS.values():
        if t.example:
            examples.add_row(t.example)
    tips = Group(
        Text("Pass extra arguments to the underlying tool with ARGS:", style="bold"),
        examples,
        Text("No make (e.g. Windows)? Run the same tasks with: python makefile.py <task> [args...]", style="dim"),
    )
    console.print(Panel(tips, title="💡 [bold]Tips[/]", title_align="left", border_style="bright_black", expand=False))
    return 0


# ---------------------------------------------------------------- entry point
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    name, args = (argv[0], argv[1:]) if argv else ("help", [])
    if name in ("-h", "--help"):
        name = "help"
    selected = TASKS.get(name)
    if selected is None:
        console.print(f"[red]❌ Unknown task:[/] [bold]{name}[/]\n")
        t_help([])
        return 2
    return selected.func(args)


if __name__ == "__main__":
    sys.exit(main())
