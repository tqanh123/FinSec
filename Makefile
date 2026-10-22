.PHONY: verify test run-api docker-up docker-down finetune-dry-run

verify:
	PYTHONPATH=src python3 tests/test_end_to_end.py

test:
	python -m pytest -q

run-api:
	PYTHONPATH=src uvicorn finsec_alpharadar.api.server:app --host 127.0.0.1 --port 8000 --reload

docker-up:
	docker compose --profile ticks --profile rag up -d

docker-down:
	docker compose --profile ticks --profile rag down

finetune-dry-run:
	PYTHONPATH=src python3 scripts/train_unsloth_slm.py --dry-run
