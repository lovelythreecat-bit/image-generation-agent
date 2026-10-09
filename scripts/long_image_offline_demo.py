"""Run the complete long-image pipeline against deterministic offline HTTP fixtures.

No credentials or external network are used. Pillow drawings are provider fixtures;
the production planner validation, product evidence, graph, store and renderer run.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

from image_agent import create_long_image, long_result_to_bundle
from image_agent.config import AgentConfig
from image_agent.long_image_models import LongImageRequest

SCENES = ("product_detail", "brand_intro", "promotion", "tutorial", "comparison", "custom")
TITLES = {
    "product_detail": "青瓷杯 · 日常饮水",
    "brand_intro": "青叶工作室 · 品牌介绍",
    "promotion": "秋日分享会 · 活动邀请",
    "tutorial": "桌面整理 · 三步指南",
    "comparison": "两种整理方式 · 对比说明",
    "custom": "给生活留一点空间",
}


def illustration(scene, *, detail=False, ratio="16:9"):
    """Distinct local material/provider PNGs with a deliberate supported aspect."""
    a, b = map(int, ratio.split(":"))
    width, height = (1280, 1280 * b // a) if a >= b else (1280 * a // b, 1280)
    canvas = Image.new("RGB", (width, height), "#eee9dd")
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((width * 0.12, height * 0.16, width * 0.88, height * 0.9), fill="#ddd8c8")
    if scene == "product_detail":
        x, y = width * 0.28, height * 0.25
        draw.ellipse(
            (x + width * 0.3, y + height * 0.1, x + width * 0.52, y + height * 0.43),
            fill="#668b78",
            outline="#2c5142",
            width=16,
        )
        draw.ellipse(
            (x + width * 0.32, y + height * 0.15, x + width * 0.47, y + height * 0.38),
            fill="#eee9dd",
        )
        draw.rounded_rectangle(
            (x, y, x + width * 0.38, y + height * 0.49),
            radius=60,
            fill="#89ab97",
            outline="#2c5142",
            width=8,
        )
        draw.ellipse(
            (x, y - height * 0.035, x + width * 0.38, y + height * 0.085),
            fill="#c8d9cc",
            outline="#2c5142",
            width=8,
        )
        draw.line((x + 30, y + height * 0.12, x + 30, y + height * 0.4), fill="#c8d9cc", width=12)
        if detail:
            draw.arc(
                (x + 30, y + 40, x + width * 0.33, y + height * 0.42),
                20,
                130,
                fill="#355d4b",
                width=18,
            )
    else:
        colors = ("#779d88", "#d19466", "#9ab4bf")
        for index, color in enumerate(colors):
            x = width * (0.13 + index * 0.25)
            y = height * (0.22 + (index % 2) * 0.13)
            draw.rounded_rectangle((x, y, x + width * 0.2, y + height * 0.4), radius=26, fill=color)
            draw.line((x + 24, y + 40, x + width * 0.16, y + 40), fill="#fff9e9", width=12)
            draw.ellipse(
                (x + 26, y + height * 0.13, x + 85, y + height * 0.13 + 59), fill="#fff9e9"
            )
    output = io.BytesIO()
    canvas.save(output, "PNG")
    canvas.close()
    return output.getvalue()


def make_request(scene, out, font_path=None):
    request = {
        "scene": scene,
        "title": TITLES[scene],
        "content_brief": "组织六段内容：主题、说明、要点、步骤、参数、收尾。"
        "活动日期为2026年10月12日。必须展示“原样保留：１０月１２日，09:30”。",
        "style_hint": "浅色背景、青绿色强调，清晰的标题与正文层级",
        "required_copy": [
            {"requirement_id": "exact-copy", "text": "保持原文\n第二行：ＡＢＣ 123"},
            {"requirement_id": "same-title", "text": TITLES[scene]},
        ],
        "options": {
            "width": 750,
            "slice_height": 1000,
            "theme": "warm" if scene == "promotion" else "clean",
            "font_path": font_path,
        },
        "image_size": "1K",
        "output_dir": Path(out),
    }
    if scene == "product_detail":
        request["product_specs"] = [
            {"name": "容量", "value": "３５０ mL"},
            {"name": "活动日期", "value": "2026年10月12日"},
        ]
        request["service_notes"] = ["使用后清洁并自然晾干。"]
        request["product_context"] = {
            "product_name": "青瓷杯",
            "category": "饮水杯",
            "presentation_mode": "product_only",
        }
        request["materials"] = [
            {
                "material_id": "m1",
                "source": {"data": illustration(scene, ratio="1:1")},
                "subject_hint": "绿色有柄陶瓷杯的完整外观",
            },
            {
                "material_id": "m2",
                "source": {"data": illustration(scene, detail=True, ratio="1:1")},
                "subject_hint": "同一绿色陶瓷杯的釉面细节",
            },
        ]
    else:
        table = {
            "brand_intro": ("理念", "清晰表达", "形式", "文字与插图"),
            "promotion": ("活动日期", "2026年10月12日", "参与方式", "现场交流"),
            "tutorial": ("准备", "清单与收纳盒", "检查", "３ 项逐一核对"),
            "comparison": ("方式", "说明", "顺序整理", "３ 步逐项完成"),
            "custom": ("阅读方式", "先整体后细节", "检查项目", "３ 项内容"),
        }[scene]
        request["required_copy"] = [
            {"requirement_id": f"table-{index}", "text": text} for index, text in enumerate(table)
        ] + request["required_copy"]
    return LongImageRequest.model_validate(request)


def offline_config():
    return AgentConfig.from_mapping(
        {
            "services": {
                "vision": {
                    "base_url": "https://offline.invalid",
                    "protocol": "chat_completions",
                    "api_key": "offline-local-fixture",
                },
                "image": {
                    "base_url": "https://offline.invalid",
                    "protocol": "openai_images",
                    "api_key": "offline-local-fixture",
                },
            },
            "image_models": {"pro": {"service": "image", "model": "offline-image"}},
            "stages": {
                "vision": {"service": "vision", "model": "offline-discovery"},
                "plan_long_image": {"service": "vision", "model": "offline-plan"},
                "audit_long_image_asset": {"service": "vision", "model": "offline-audit"},
            },
        }
    )


def instruction_object(text, key):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(value, dict) and key in value:
                return value
    raise AssertionError(f"provider request missing {key}")


def plan_response(data, mode="generate"):
    request = data["request"]
    requirements = data["requirements"]
    sections = [
        {"section_id": "hero", "role": "hero", "text_units": []},
        {
            "section_id": "intro",
            "role": "image_text",
            "text_units": [
                {"unit_id": "intro-heading", "kind": "heading", "text": "从主题开始"},
                {
                    "unit_id": "intro-body",
                    "kind": "body",
                    "text": "把信息放在合适的位置。图片帮助理解，文字清楚传达每一个细节。",
                },
            ],
        },
        {
            "section_id": "points",
            "role": "bullets",
            "text_units": [
                {"unit_id": "points-heading", "kind": "heading", "text": "值得关注的要点"},
                {"unit_id": "point-1", "kind": "bullet", "text": "先看整体，再了解细节。"},
                {"unit_id": "point-2", "kind": "bullet", "text": "重要信息保持原文，方便核对。"},
            ],
        },
        {
            "section_id": "steps",
            "role": "steps",
            "text_units": [
                {"unit_id": "steps-heading", "kind": "heading", "text": "按顺序完成"},
                {"unit_id": "step-1", "kind": "body", "text": "准备：阅读说明，整理需要的物品。"},
                {"unit_id": "step-2", "kind": "body", "text": "执行：按照介绍逐项操作。"},
                {"unit_id": "step-3", "kind": "body", "text": "检查：确认每一项信息都已完整展示。"},
            ],
        },
        {
            "section_id": "table",
            "role": "table",
            "text_units": [{"unit_id": "table-heading", "kind": "heading", "text": "信息一览"}],
            "table_rows": [],
        },
        {"section_id": "closing", "role": "closing", "text_units": []},
    ]
    cells = []
    for index, requirement in enumerate(requirements):
        pointer = requirement["source"]["pointer"]
        section = (
            sections[0]
            if pointer == "/title"
            else sections[4]
            if pointer.startswith("/product_specs/")
            or requirement["requirement_id"].startswith("table-")
            else sections[5]
        )
        for occurrence in range(requirement["occurrences"]):
            unit = {
                "unit_id": f"required-{index}-{occurrence}",
                "kind": "heading" if pointer == "/title" else "body",
                "text": requirement["text"],
                "source": requirement["source"],
                "requirement_ids": [requirement["requirement_id"]],
            }
            if section is sections[4]:
                unit["kind"] = "cell"
                cells.append(unit)
            else:
                section["text_units"].append(unit)
    sections[4]["table_rows"] = [cells[index : index + 2] for index in range(0, len(cells), 2)]
    if not cells:
        sections[4]["table_rows"] = [
            [
                {"unit_id": "cell-name", "kind": "cell", "text": "项目"},
                {"unit_id": "cell-value", "kind": "cell", "text": "说明"},
            ]
        ]
    if not sections[5]["text_units"]:
        sections[5]["text_units"] = [
            {"unit_id": "end", "kind": "body", "text": "逐项检查，完成介绍。"}
        ]
    slots = []
    if mode != "text":
        material_ids = [m["material_id"] for m in data["materials"]]
        source = mode == "source"
        slots = [
            {
                "slot_id": "shared",
                "source": "source" if source else "generate",
                "material_ids": material_ids[:1] if source else material_ids,
                "role": "hero" if request["scene"] == "product_detail" else "illustration",
                "prompt": ""
                if source
                else "绿色陶瓷杯完整外观与柔和自然光"
                if request["scene"] == "product_detail"
                else "简洁几何物件组成的步骤示意插画，无文字",
                "aspect_ratio": None
                if source
                else "1:1"
                if request["scene"] == "product_detail"
                else "16:9",
                "section_ids": ["hero", "intro"],
            }
        ]
        sections[0]["slot_ids"] = sections[1]["slot_ids"] = ["shared"]
    return {
        "status": "ready",
        "plan": {
            "input_fingerprint": data["input_fingerprint"],
            "scene": request["scene"],
            "theme": data["theme"],
            "visual_direction": "浅米色背景、青绿与陶土色、柔和光线",
            "requirements": requirements,
            "sections": sections,
            "image_slots": slots,
            "analysis_ref": data["analysis_ref"],
            "resolved_selection": data["resolved_selection"],
        },
    }


class OfflineProvider:
    def __init__(self, request, *, mode="generate", audit="pass", malformed_plan=False):
        self.request, self.mode, self.audit = request, mode, audit
        self.malformed_plan = malformed_plan
        self.posts = []

    @property
    def image_posts(self):
        return sum("/images/" in path for path, _ in self.posts)

    def respond(self, http_request):
        assert http_request.method == "POST" and http_request.url.host == "offline.invalid"
        path = http_request.url.path
        if "/images/" in path:
            self.posts.append((path, "image"))
            ratio = "1:1" if self.request.scene == "product_detail" else "16:9"
            image = illustration(self.request.scene, detail=True, ratio=ratio)
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(image).decode()}]}
            )
        payload = json.loads(http_request.content)
        model = payload["model"]
        self.posts.append((path, model))
        text = payload["messages"][0]["content"][0]["text"]
        if model == "offline-plan":
            value = plan_response(instruction_object(text, "input_fingerprint"), self.mode)
            if self.malformed_plan:
                value["plan"]["sections"][0]["text_units"][0]["text"] = "被错误替换的标题"
        elif "expected_check_ids" in text:
            data = instruction_object(text, "expected_check_ids")
            expected = data["expected_check_ids"]
            value = {
                "subject_checks": [
                    {"subject_id": item, "same_product": True, "score": 95, "reason": "外观一致"}
                    for item in expected.get("subject_id", [])
                ],
                "fact_checks": [
                    {"fact_id": item, "presence": "present", "fidelity_score": 95, "reason": "可见"}
                    for item in expected.get("fact_id", [])
                ],
            }
            if "issue_id" in expected:
                value.update(
                    outcome="verified",
                    intent_valid=True,
                    issue_resolutions=[
                        {"issue_id": item, "status": "resolved", "reason": "已核对"}
                        for item in expected["issue_id"]
                    ],
                )
            else:
                value.update(
                    element_checks=[
                        {
                            "element_id": item,
                            "presence": "present",
                            "fidelity_score": 95,
                            "reason": "保留",
                        }
                        for item in expected["element_id"]
                    ],
                    constraint_checks=[
                        {"constraint_id": item, "satisfied": True, "reason": "符合"}
                        for item in expected["constraint_id"]
                    ],
                    visual_quality=95,
                    platform_compliance=95,
                    garment_fusion=95,
                    model_preference=95,
                    output_intent=95,
                    passed=True,
                    reason="离线外观审核通过",
                )
        elif model == "offline-audit":
            if self.audit == "error":
                return httpx.Response(400, json={"error": "offline audit unavailable"})
            data = instruction_object(text, "candidate_id")
            passed = self.audit == "pass"
            value = {
                "slot_id": data["slot"]["slot_id"],
                "candidate_id": data["candidate_id"],
                "checks": [
                    {
                        "kind": kind,
                        "passed": passed,
                        "reason": "离线构图检查",
                        "severity": "info" if passed else "critical",
                    }
                    for kind in ("purpose", "subject", "style", "unrequested_text")
                ],
                "passed": passed,
                "repairable": not passed,
                "repair_prompt": None if passed else "改善配图构图与光线",
            }
        else:
            mids = [m.material_id for m in self.request.materials]
            value = {
                "analysis_id": "offline-analysis",
                "fingerprint": "bound-by-production",
                "status": "ready",
                "materials": [
                    {
                        "material_id": mid,
                        "sha256": "bound-by-production",
                        "observed_role": "identity",
                        "summary": "绿色陶瓷杯",
                    }
                    for mid in mids
                ],
                "subjects": [
                    {
                        "subject_id": "cup",
                        "material_ids": mids,
                        "identity_fact_ids": ["green-cup"],
                        "representative_material_id": mids[0],
                        "matches_product": True,
                    }
                ],
                "facts": [
                    {
                        "fact_id": "green-cup",
                        "subject_id": "cup",
                        "verifiability": "visible_appearance",
                        "description": "绿色有柄陶瓷杯",
                        "confidence": 0.95,
                        "evidence": [
                            {
                                "material_id": mids[0],
                                "observation": "绿色杯体及把手可见",
                                "source_type": "visual",
                            }
                        ],
                    }
                ],
                "elements": [],
                "intent": {
                    "proposal": {
                        "primary_subject_id": "cup",
                        "subject_ids": ["cup"],
                        "required_element_ids": [],
                        "preferred_element_ids": [],
                        "excluded_element_ids": [],
                    },
                    "constraints": [],
                    "focus_element_ids": [],
                    "unmet_requirements": [],
                },
                "issues": [],
                "warnings": [],
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
        )


@contextmanager
def offline_http(provider):
    client_type = httpx.AsyncClient

    def client(**kwargs):
        kwargs.pop("transport", None)
        return client_type(transport=httpx.MockTransport(provider.respond), **kwargs)

    with patch("httpx.AsyncClient", client):
        yield provider


def verify_delivery(result):
    """Check decoded artifacts, literal coverage, saved hashes and public metadata."""
    assert result.status == "succeeded", result.error_info
    artifacts = [*result.sections, result.page, *result.slices]
    for artifact in artifacts:
        assert artifact.availability == "current" and artifact.status == "succeeded"
        assert hashlib.sha256(artifact.image).hexdigest() == artifact.sha256
        assert (Path(result.run_dir) / artifact.file_path).read_bytes() == artifact.image
        with Image.open(io.BytesIO(artifact.image)) as decoded:
            assert decoded.size == (artifact.width, artifact.height)
    assert all(check["passed"] for check in result.page_checks)
    with Image.open(io.BytesIO(result.page.image)) as page:
        restored = Image.new("RGB", page.size)
        top = 0
        for artifact in result.slices:
            with Image.open(io.BytesIO(artifact.image)) as part:
                restored.paste(part, (0, top))
                top += part.height
        assert top == page.height and restored.tobytes() == page.convert("RGB").tobytes()
        assert len(page.getcolors(page.width * page.height)) > 100
        restored.close()
    for requirement in result.plan.requirements:
        drawn = [
            unit
            for unit in result.page.layout["text_units"]
            if requirement.requirement_id in unit["requirement_ids"]
        ]
        assert len(drawn) == requirement.occurrences
        assert all(unit["text"] == requirement.text for unit in drawn)
    bundle = long_result_to_bundle(result)
    public = bundle.dto.model_dump_json()
    assert "file_path" not in public and "run_dir" not in public and "ledger_ref" not in public
    assert str(Path(result.run_dir)) not in public
    for blob_id, data in bundle.blobs.items():
        assert blob_id.startswith("long_image/") and data.startswith(b"\x89PNG")
    public_images = [
        *bundle.dto.sections,
        bundle.dto.page,
        *bundle.dto.slices,
        *bundle.dto.image_assets,
        *(c for asset in bundle.dto.image_assets for c in asset.candidates),
    ]
    for item in public_images:
        if item.blob_id is not None:
            data = bundle.blobs[item.blob_id]
            assert item.byte_length == len(data) and item.mime_type == "image/png"
            assert item.sha256 == hashlib.sha256(data).hexdigest()
    return bundle


async def run_demo(out, font_path=None):
    out = Path(out).resolve()
    summary = {}
    for scene in ("product_detail", "promotion", "tutorial"):
        request = make_request(scene, out / scene, font_path)
        provider = OfflineProvider(request)
        with offline_http(provider):
            result = await create_long_image(request, offline_config())
        verify_delivery(result)
        summary[scene] = {
            "run_dir": result.run_dir,
            "result_json": str(Path(result.run_dir) / "result.json"),
            "page": str(Path(result.run_dir) / result.page.file_path),
            "sections": [str(Path(result.run_dir) / a.file_path) for a in result.sections],
            "slices": [str(Path(result.run_dir) / a.file_path) for a in result.slices],
            "posts": provider.posts,
            "usage": result.usage.model_dump(mode="json"),
        }
    out.mkdir(parents=True, exist_ok=True)
    (out / "demo-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("out/long-image-offline-demo"))
    parser.add_argument("--font-path", type=Path)
    args = parser.parse_args()
    summary = asyncio.run(run_demo(args.out, args.font_path))
    for scene, paths in summary.items():
        print(f"{scene}: {paths['page']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
