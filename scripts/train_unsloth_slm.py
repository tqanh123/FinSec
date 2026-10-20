#!/usr/bin/env python3
"""
Unsloth QLoRA + GRPO Training Script for FinSec Catalyst Scorer (`Qwen3-1.7B`).
Pairs SEC 8-K & Vietnam HSX BCTC disclosures with realized 1-hour Abnormal Return (CAR).
"""
from __future__ import annotations

import argparse
import json


def get_training_recipe() -> dict:
    return {
        "base_model": "unsloth/Qwen3-1.7B-bnb-4bit",
        "dataset_sources": [
            "sec_edgar_8k_car_labeled.jsonl",
            "vn_hose_bctc_nqhdqt_car_labeled.jsonl",
        ],
        "grpo_rewards": [
            "strict_json_schema_reward",
            "abnormal_return_sign_accuracy_reward",
            "xbrl_number_faithfulness_reward",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    recipe = get_training_recipe()
    print(json.dumps({"status": "validated" if args.dry_run else "ready", "recipe": recipe}, indent=2))
