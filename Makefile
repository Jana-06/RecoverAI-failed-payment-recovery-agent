# RecoverAI — one-command demo. (Recipes require TABS; do not convert to spaces.)
.PHONY: demo seed test clean

PY ?= python

demo:            ## seed (if needed) + start the dashboard at http://localhost:8000
	$(PY) -m app.seed --force
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

seed:            ## rebuild the deterministic synthetic dataset
	$(PY) -m app.seed --force

test:            ## run the full pytest suite
	$(PY) -m pytest

clean:           ## remove local demo DBs and caches
	rm -rf data/*.db .pytest_cache **/__pycache__
