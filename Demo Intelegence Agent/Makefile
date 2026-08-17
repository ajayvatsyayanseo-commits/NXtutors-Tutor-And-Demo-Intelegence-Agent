.PHONY: install format lint type test test-security contracts e2e audit security check doctor migrate clean

install:
	uv sync --all-extras

format:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

lint:
	uv run ruff format --check src tests scripts
	uv run ruff check src tests scripts

type:
	uv run mypy src

test:
	uv run pytest --cov=demo_command_center --cov-report=term-missing

test-security:
	uv run pytest -m security -q

contracts:
	uv run pytest -m contract -q

e2e:
	uv run pytest -m e2e -q

audit:
	uv export --format requirements-txt --all-extras --no-emit-project --no-hashes \
		-o requirements.audit.txt
	uv run pip-audit --strict -r requirements.audit.txt

security:
	uv run bandit -r src -ll -c pyproject.toml
	uv run ruff check --select S src

# The one gate. Everything a change must pass.
check: lint type test contracts

# Full demo lifecycle end to end against in-memory adapters. No AWS, no OpenAI,
# no Google, no Cashfree credentials required.
demo:
	uv run dcc-e2e

doctor:
	uv run dcc-doctor

migrate:
	uv run dcc-migrate

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
