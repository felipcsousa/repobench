.PHONY: install install-dev build clean publish test lint

# Install for users
install:
	pip install .

# Install for development
install-dev:
	pip install -e ".[dev]"

# Install with pipx (recommended for CLI tools)
install-pipx:
	pipx install .

# Build package
build: clean
	python -m build

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info repobench/*.pyc

# Publish to PyPI (test)
publish-test: build
	twine upload --repository testpypi dist/*

# Publish to PyPI (production)
publish: build
	twine upload dist/*

# Run tests
test:
	pytest tests/ -v

# Run linter
lint:
	ruff check repobench/
	ruff format repobench/ --check

# Format code
format:
	ruff check repobench/ --fix
	ruff format repobench/

# Type check
typecheck:
	mypy repobench/

# Show package info
info:
	@echo "Package: repobench"
	@echo "Version: $$(python -c 'from repobench import __version__; print(__version__)')"
	@echo "Entry point: repobench = repobench.cli.app:app"
