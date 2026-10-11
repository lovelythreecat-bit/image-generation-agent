"""A new retry policy must not invalidate saved product or long-image inputs."""

from image_agent.config import AgentConfig
from image_agent.run_store import RunStore, config_fingerprint


def test_completion_attempt_limit_can_change_when_loading_saved_execution():
    config = AgentConfig(model_completion_attempts=4)
    store = RunStore()
    store.json(
        "run.json",
        {"version": 2, "config_fingerprint": config_fingerprint(config), "inputs": []},
    )
    metadata = store.load_metadata(AgentConfig(model_completion_attempts=6))
    assert metadata["version"] == 2


def test_default_configuration_retains_fingerprint_of_precompletion_version():
    # Hand-written historical payload from the fields present before this change.
    import hashlib
    import json

    legacy_payload = {
        "image_models": {},
        "stages": {},
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "model_pro": "google/gemini-3.1-flash-image",
        "model_fast": "openai/gpt-image-2",
        "model_base": "qwen/qwen-image-3-pro",
        "quality_model": "openai/gpt-4o-mini",
        "style_model": "openai/gpt-4o-mini",
        "generation_reference_limit": 4,
        "vision_image_limit": 12,
        "services": {},
    }
    historical_hash = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True).encode()
    ).hexdigest()
    assert config_fingerprint(AgentConfig()) == historical_hash
