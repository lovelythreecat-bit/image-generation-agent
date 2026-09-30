def analysis_data():
    return {
        "schema_version": "1.0",
        "analysis_id": "a1",
        "fingerprint": "hash",
        "status": "ready",
        "materials": [
            {
                "material_id": "m1",
                "sha256": "hash",
                "observed_role": "identity",
                "summary": "ceramic cup",
            }
        ],
        "subjects": [
            {
                "subject_id": "s1",
                "material_ids": ["m1"],
                "identity_fact_ids": ["f1"],
                "representative_material_id": "m1",
                "matches_product": True,
            }
        ],
        "facts": [
            {
                "fact_id": "f1",
                "verifiability": "visible_appearance",
                "subject_id": "s1",
                "description": "white ceramic cup",
                "evidence": [
                    {
                        "material_id": "m1",
                        "observation": "white cup visible",
                        "source_type": "visual",
                    }
                ],
                "confidence": 0.95,
            }
        ],
        "elements": [
            {
                "element_id": "e1",
                "kind": "detail",
                "subject_id": "s1",
                "description": "ceramic texture",
                "fact_ids": ["f1"],
            }
        ],
        "intent": {
            "proposal": {
                "primary_subject_id": "s1",
                "subject_ids": ["s1"],
                "required_element_ids": [],
                "preferred_element_ids": ["e1"],
                "excluded_element_ids": [],
            },
            "constraints": [],
            "focus_element_ids": ["e1"],
            "unmet_requirements": [],
        },
        "issues": [],
        "warnings": [],
        "error_info": None,
    }


class FakeVision:
    """Scripted external results; selection, plans and local evaluation stay real."""

    def __init__(self, *, scores=None, discovery=None):
        self.scores = scores or {}
        self.discovery = discovery
        self.calls = []
        self.audits = {}

    async def analyze_materials(self, request, materials):
        from image_agent.models import MaterialAnalysis

        self.calls.append("discover")
        return MaterialAnalysis(**(self.discovery or analysis_data()))

    async def validate_evidence(self, request, materials, analysis, draft):
        from image_agent.models import EvidenceValidation
        from image_agent.vision import selected_fact_ids

        self.calls.append("evidence")
        return EvidenceValidation(
            outcome="verified",
            subject_checks=[
                dict(subject_id=s, score=95, same_product=True, reason="same")
                for s in draft.candidate.subject_ids
            ],
            fact_checks=[
                dict(fact_id=f, presence="present", fidelity_score=95, reason="same")
                for f in selected_fact_ids(analysis, draft.candidate)
            ],
            intent_valid=True,
            issue_resolutions=[
                dict(issue_id=i.issue_id, status="resolved", reason="verified")
                for i in draft.pending_issues
            ],
        )

    async def audit_image(self, request, target, context, plan, image):
        from image_agent.models import GeneratedImageAudit

        self.calls.append("audit:" + target.target_key)
        key = target.variant or "main"
        n = self.audits.get(key, 0)
        self.audits[key] = n + 1
        script = self.scores.get(key, [{}])
        override = script[min(n, len(script) - 1)]
        if isinstance(override, BaseException):
            raise override
        d = dict(
            subject_checks=[],
            fact_checks=[],
            element_checks=[],
            constraint_checks=[],
            visual_quality=90,
            platform_compliance=90,
            garment_fusion=90,
            model_preference=90,
            output_intent=90,
            passed=True,
            reason="ok",
        )
        for r in plan.requirements:
            if r.target_kind == "subject":
                row = dict(
                    subject_id=r.target_id,
                    score=override.get("identity", 90),
                    same_product=override.get("same", True),
                    reason="same",
                )
            elif r.target_kind == "constraint":
                row = dict(constraint_id=r.target_id, satisfied=True, reason="ok")
            else:
                present = r.applicability != "not_applicable"
                row = {
                    r.target_kind + "_id": r.target_id,
                    "presence": "present" if present else "not_applicable",
                    "fidelity_score": 90 if present else None,
                    "reason": "ok",
                }
            d[r.target_kind + "_checks"].append(row)
        d.update({k: v for k, v in override.items() if k not in ("identity", "same")})
        return GeneratedImageAudit(**d)

    async def review_product(self, context, plan, subject_ids, image):
        from image_agent.models import FocusedProductAudit

        self.calls.append("review")
        return FocusedProductAudit(
            subject_checks=[
                dict(subject_id=s, score=90, same_product=True, reason="same") for s in subject_ids
            ]
        )

    async def describe_style(self, request, references):
        self.calls.append("style")
        return "A neutral soft style."

    async def extract_garments(self, request, materials, subject_id):
        from image_agent.models import GarmentStructureAudit

        self.calls.append("garments:" + subject_id)
        return GarmentStructureAudit(items=[])

    async def audit_detail_set(self, platform, context, plans, images):
        from image_agent.models import DetailSetAudit

        self.calls.append("set")
        return DetailSetAudit(
            platform=platform,
            passed=True,
            distinctiveness=90,
            role_coverage=90,
            issues=[],
            reason="ok",
        )


class FakeGenerator:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def generate(self, **kwargs):
        from tests.test_images import picture

        self.calls.append(kwargs)
        if self.error:
            raise self.error
        a, b = map(int, kwargs["aspect_ratio"].split(":"))
        return picture((int(1600 * a / max(a, b)), int(1600 * b / max(a, b))))
