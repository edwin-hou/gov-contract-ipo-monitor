.PHONY: install test check run once docker

install:
	python -m pip install -e ".[dev]"

test:
	pytest

check: test
	python -m compileall -q src
	gov-contract-ipo-monitor --help >/dev/null

run:
	gov-contract-ipo-monitor run

once:
	gov-contract-ipo-monitor run-once

docker:
	docker compose up --build
