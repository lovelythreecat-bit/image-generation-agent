def analysis_data():
    return {
        "schema_version": "1.0", "analysis_id": "a1", "fingerprint": "hash", "status": "ready",
        "materials": [{"material_id": "m1", "sha256": "hash", "observed_role": "identity", "summary": "ceramic cup"}],
        "subjects": [{"subject_id": "s1", "material_ids": ["m1"], "identity_fact_ids": ["f1"], "representative_material_id": "m1", "matches_product": True}],
        "facts": [{"fact_id": "f1", "subject_id": "s1", "description": "white ceramic cup", "evidence": [{"material_id": "m1", "observation": "white cup visible"}], "confidence": 0.95}],
        "elements": [{"element_id": "e1", "kind": "detail", "subject_id": "s1", "description": "ceramic texture", "fact_ids": ["f1"]}],
        "intent": {"proposal": {"primary_subject_id": "s1", "subject_ids": ["s1"], "required_element_ids": [], "preferred_element_ids": ["e1"], "excluded_element_ids": []}, "constraints": [], "focus_element_ids": ["e1"], "unmet_requirements": []},
        "issues": [], "warnings": [], "error_info": None,
    }
