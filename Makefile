.PHONY: install run test wipe

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
AIM := $(VENV)/bin/aim

install:
	rm -rf $(VENV) build dist *.egg-info
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e . pytest

run:
	$(AIM)

test:
	$(PY) -m compileall aim
	$(PY) -m pytest

wipe:
	docker rm -f $$(docker ps -aq --filter label=aim.managed=1) 2>/dev/null || true
	docker images --format '{{.Repository}}:{{.Tag}}' | grep '^aim-' | xargs -r docker rmi
	rm -rf $(VENV) .aim ~/.aim build dist *.egg-info .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
