# pyocr-toolkit developer commands.
#
# Every target is a thin alias for `python makefile.py <target>`, where the real logic and the rich output live
# (so Windows users without `make` can run the same tasks). Run `make` or `make help` for the grouped list.
#
# Pass extra arguments to the underlying tool with ARGS, e.g.
#   make test ARGS="-k compare"
#   make serve ARGS="--port 9000 --open"
#
# Use a specific interpreter (e.g. your venv) with PYTHON=.venv/bin/python.

PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)
ARGS   ?=
TASK    = @$(PYTHON) makefile.py $@ $(ARGS)

.DEFAULT_GOAL := help

# 🛠️  Setup ------------------------------------------------------------------
.PHONY: install init doctor providers
# install runs pip directly so it works before rich (a dev dependency) is installed.
install:
	$(PYTHON) -m pip install -e ".[dev]" $(ARGS)
init doctor providers:
	$(TASK)

# 🧪 Quality -----------------------------------------------------------------
.PHONY: lint fix test check
lint fix test check:
	$(TASK)

# 🚀 Run ---------------------------------------------------------------------
.PHONY: serve dev site
serve dev site:
	$(TASK)

# 📦 Build & Release ---------------------------------------------------------
.PHONY: build version
build version:
	$(TASK)

# 🐳 Docker ------------------------------------------------------------------
.PHONY: docker-build docker-up docker-down docker-logs
docker-build docker-up docker-down docker-logs:
	$(TASK)

# 🧰 Info & Housekeeping -----------------------------------------------------
.PHONY: info clean help
info clean help:
	$(TASK)
