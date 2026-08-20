.PHONY: install format lint type test test-unit test-security contracts audit security check e2e doctor migrate env-example run clean verify verify-fast

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
	uv run pytest --cov=tutor_match_meta --cov-report=term-missing

test-unit:
	uv run pytest tests/unit -q

test-security:
	uv run pytest -m security -q

contracts:
	uv run pytest -m contract -q

# Dependency vulnerability scan over the exported lockfile — the same thing CI
# gates on. Auditing the installed environment instead would fail on the
# editable local package, which is not a dependency anyone ships.
audit:
	uv export --format requirements-txt --all-extras --no-emit-project --no-hashes \
		-o requirements.audit.txt
	uv run pip-audit --strict -r requirements.audit.txt

# Security scanners that gate CI. Run before pushing.
security:
	uv run bandit -r src -ll -c pyproject.toml
	uv run ruff check --select S src

# `.env.example` is generated from Settings; regenerate after adding a field.
env-example:
	uv run python scripts/sync_env_example.py

# The one gate. Everything a PR must pass.
# The whole agent — both halves and the seam between them — in one command.
# Two separate suites made it easy to leave the second one red for a week
# without noticing, and the parts that only exist *between* the halves (shared
# config, shared database, the handoff) had no gate at all.
verify:
	uv run python scripts/verify_agent.py

# Same, minus anything needing the live database or the network.
verify-fast:
	uv run python scripts/verify_agent.py --fast

check: lint type test contracts

# Full local end-to-end sample conversation, zero AWS/OpenAI credentials needed.
e2e:
	uv run tutor-match-e2e

doctor:
	uv run tutor-match-doctor

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn tutor_match_meta.api.app:app --app-dir src --reload

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
