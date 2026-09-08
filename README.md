# MOS

A minimal implementation of the **Meme Operating System (MOS)**. The system
models "memes" as units of information that can mutate, replicate and compete
within a small network.

## Components

* `mos.meme.Meme` - represents a single meme of various types (code, text, data,
  images, or neural network model). A meme can mutate, replicate and execute.
* `mos.network.MemeNetwork` - manages a collection of memes and provides a
  simple evolutionary cycle.

The implementation is intentionally lightweight but can serve as a starting
point for experiments with memetic algorithms.

## MOS R4.2 Portable

`runtime/portable` contains the complete executable MOS runtime for embedded
use inside ChatGPT, Claude, or another tool-enabled LLM chat. GitHub stores the
canonical code and the `mos-state` branch stores the portable hash-verified
state. The chat's Python environment executes MOS before and after the host
model drafts an answer; no external LLM API is required.

Start with [`runtime/portable/README_RU.md`](runtime/portable/README_RU.md) and
the host-specific files in [`runtime/portable/starters`](runtime/portable/starters).
