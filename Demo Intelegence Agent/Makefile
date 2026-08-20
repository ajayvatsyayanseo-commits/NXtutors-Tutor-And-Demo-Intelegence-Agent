.PHONY: install format lint type test test-security contracts e2e audit security check templates doctor demo sync chat migrate clean

# `uv run` when the project has its own environment; `RUN` can be overridden to
# use the shared repository venv, which is what the monorepo checkout does:
#
#     make check RUN='../.venv/Scripts/python.exe -m'
#
# Kept as a variable rather than hardcoded because CI and a developer laptop
# disagree about which of the two exists.
RUN ?= uv run python -m

install:
	uv sync --all-extras

format:
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

lint:
	$(RUN) ruff format --check src tests
	$(RUN) ruff check src tests

type:
	$(RUN) mypy src

test:
	$(RUN) pytest --cov=demo_command_center --cov-report=term-missing

test-security:
	$(RUN) pytest -m security -q

contracts:
	$(RUN) pytest -m contract -q

e2e:
	$(RUN) pytest -m e2e -q

audit:
	uv export --format requirements-txt --all-extras --no-emit-project --no-hashes \
		-o requirements.audit.txt
	$(RUN) pip_audit --strict -r requirements.audit.txt

security:
	$(RUN) bandit -r src -ll -c pyproject.toml
	$(RUN) ruff check --select S src

# The one gate. Everything a change must pass before it is proposed.
# Note e2e and test-security are included: the lifecycle and the money path are
# not optional extras, and a gate that skips them is a gate that passes a broken
# payment flow.
check: lint type test contracts e2e test-security templates

# Full demo lifecycle end to end against in-memory adapters. No AWS, no OpenAI,
# no Google, no Cashfree, no Meta credentials required.
demo:
	$(RUN) demo_command_center.cli.local_e2e

# Both agents as one product: the REAL tutor_match_meta orchestrator driving the
# real Demo lifecycle, plus the sync invariants. Needs `../src` on the path,
# which `RUN` already provides in the monorepo checkout. Fails rather than
# falling back to the fake — a silent fallback would print thirty green steps
# while proving nothing about the integration.
sync:
	$(RUN) demo_command_center.cli.combined_e2e

# Every approved template: declared, bindable, reachable, and — the check that
# was missing — able to survive the output guard. A template failure is the
# quietest failure here: the send is refused, the reminder never arrives, and
# nothing turns red. So it runs inside `check`, not on demand.
#
# `scripts/` is not a package, so this runs the file rather than a module.
templates:
	$(RUN:-m=) scripts/verify_templates.py

doctor:
	$(RUN) demo_command_center.cli.doctor

migrate:
	$(RUN) demo_command_center.cli.migrate --dry-run

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov requirements.audit.txt
