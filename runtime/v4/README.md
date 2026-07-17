# MOS persistent runtime 4.1

This directory is the durable, public-safe runtime used by GitHub Actions.

- `state.json` is the checkpoint.
- `status.json` is the latest execution receipt.
- `runtime_payload.zip.b64` contains the public-safe executable source and benchmark.
- the same consumed data epoch cannot create a second generation.
- a new release requires a new confirmed dataset hash and all release gates.

Current public-safe epoch SHA-256: `8d28009d62fbdef3532d085978af27404b1d9d3afbf3473b6c2d449d5ccb6650`.
