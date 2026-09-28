# Provider protocol checkpoint — 2026-09-29

Read-only reference: https://openrouter.ai/docs/guides/overview/multimodal/image-generation (retrieved 2026-09-29).

The official guide describes POST `/api/v1/images`, `model`, `prompt`, `n`, `input_references`, `resolution` tiers including 1K/2K/4K, and `aspect_ratio`. Its GPT Image 2 example uses `quality: high`, `aspect_ratio: 16:9`, `n: 1`. The guide exposes `/api/v1/images/models` and per-model `/endpoints` for capability discovery. This agrees with the reviewed source protocol and the package's request envelope.

Implementation retains the source response precedence: `data[].b64_json`, then `choices[].message.images`, then image data URLs in `message.content`. Invalid JSON/schema/base64 fails as a protocol error without extra creative retries. Supplier image URLs are never automatically downloaded.

Not verified: account access, default model availability, actual multi-reference limits for each endpoint, whether every selected fact is retained, quality, latency, billing, and results with 4/8 source images. Generation/vision limits are deployment declarations, not measured provider guarantees. No billable request has been made. `scripts/live_smoke.py --live` is the separate opt-in acceptance path.
