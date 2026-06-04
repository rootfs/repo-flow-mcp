.PHONY: test lint typecheck check

test:
	pytest

lint:
	ruff check src tests

typecheck:
	mypy src

check: lint typecheck test
