"""Regenerate version 1 JSON contracts from the public DTOs."""

import json
from pathlib import Path

from image_agent import CreationRequestDTO, CreationResultDTO, ErrorDTO, MaterialAnalysis


def main():
    root = Path(__file__).resolve().parents[1] / "schema"
    root.mkdir(exist_ok=True)
    for name, model in (
        ("creation-request-v1", CreationRequestDTO),
        ("material-analysis-v1", MaterialAnalysis),
        ("creation-result-v1", CreationResultDTO),
        ("error-v1", ErrorDTO),
    ):
        (root / (name + ".json")).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
