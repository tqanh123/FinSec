.PHONY: verify test run-api docker-up docker-down finetune-dry-run

verify:
	PYTHONPATH=src python3 tests/test_end_to_end.py

test:
	PYTHONPATH=src pytest -q tests/

run-api:
	PYTHONPATH=src uvicorn finsec_alpharadar.api.server:app --host 0.0.0.0 --port 8000 --reload

docker-up:
	docker compose up -d

docker-down:
	docker compose down

finetune-dry-run:
	PYTHONPATH=src python3 scripts/train_unsloth_slm.py --dry-run
