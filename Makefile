# Simple helper commands

.PHONY: run api

run:
	python scripts/run_scraper.py

api:
	uvicorn apps.api.main:app --reload
