PYTHON ?= python3

.PHONY: build lint preflight test wheel-check

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest -q

build:
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

preflight: lint test build

# Installs the freshly built wheel in a throwaway venv (no extras, no kernel) and
# proves it scaffolds and reads a memory home; add HOME_PATH=<path> to also run
# `status` against a live home.
wheel-check:
	$(PYTHON) scripts/check_installed_wheel.py $(if $(HOME_PATH),--home $(HOME_PATH),)
