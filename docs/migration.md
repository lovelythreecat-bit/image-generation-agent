# Migration provenance

Source: MediaForge, MIT, copyright 2025 MediaForge Contributors (see LICENSE).
The 12 source SHA-256 values in `superpowers/reviews/2026-09-28-source-manifest.json` were verified unchanged on 2026-09-28. No source project configuration was read or modified.

`platforms.py` preserves the complete original platform rules and public function signatures. Prompt vocabulary, shot text, compliance ordering and thresholds are derived from the reviewed source. Runtime orchestration and HTTP are independent implementations.

Intentional changes: per-asset repair; multi-material facts and selection; 480×480 white export before audit; original PNG/JPEG bytes preserved for other outputs; structured reference IDs exempt from legacy market digit replacement; platform text appended after dangerous-word checks. `product_attributes` is now keyed by subjects. JSON DTO blobs and local output manifests are separate formats. Analysis snapshots are always evidence-checked when reused.
