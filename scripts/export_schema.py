"""Export current contracts, preserving the historical result-v1 schema."""

import json
from pathlib import Path

from image_agent import CreationRequestDTO, CreationResultDTO, ErrorDTO, MaterialAnalysis
from image_agent.long_image_contracts import (
    LongImagePlanDTO,
    LongImagePlanningResultDTO,
    LongImageRequestDTO,
    LongImageResultDTO,
)


def main():
    root = Path(__file__).resolve().parents[1] / "schema"
    root.mkdir(exist_ok=True)
    for name, model in (
        ("creation-request-v1", CreationRequestDTO),
        ("material-analysis-v1", MaterialAnalysis),
        ("creation-result-v2", CreationResultDTO),
        ("error-v1", ErrorDTO),
        ("long-image-request-v1", LongImageRequestDTO),
        ("long-image-plan-v1", LongImagePlanDTO),
        ("long-image-planning-result-v1", LongImagePlanningResultDTO),
        ("long-image-result-v1", LongImageResultDTO),
    ):
        (root / (name + ".json")).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
