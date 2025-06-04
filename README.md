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
