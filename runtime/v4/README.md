# MOS persistent runtime 4.1

This directory is the durable, public-safe runtime used by GitHub Actions.

- `state.json` is the checkpoint.
- `status.json` is the latest execution receipt.
- `runtime_payload.zip.b64` contains the public-safe executable source and benchmark.
- the same consumed data epoch cannot create a second generation.
- a new release requires a new confirmed dataset hash and all release gates.
- an active genome is rejected unless its release record, source hash, configuration hash and benchmark hash all match.
- repeated runs on an already-consumed epoch are byte-stable and do not create timestamp-only commits.

Scope boundary: this runtime evolves a narrow epistemic classifier. It does not train an LLM, change model weights, prove consciousness, or establish general autonomous self-improvement.

Verified replacement state:

- active genome: `68c0ac9252799382`
- release records: `1`
- improved locked cases: `7`
- regressed locked cases: `0`
- exact McNemar p-value: `0.015625`
- source/config/data provenance: present and validated by the bundled `verify_state.py`

Current public-safe epoch SHA-256: `8d28009d62fbdef3532d085978af27404b1d9d3afbf3473b6c2d449d5ccb6650`.
