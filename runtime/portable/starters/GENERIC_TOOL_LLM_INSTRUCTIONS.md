# Portable MOS host contract

Requirements: Python 3.11+, file read/write, Git fetch, and an authenticated Git
push or a writable GitHub connector.

Per turn:

1. Fetch the exact MOS runtime and `mos-state`.
2. Write the unmodified user input to a UTF-8 file.
3. Execute `MOS_BEGIN_TURN.py` with a stable session id.
4. Refuse to continue if state verification fails.
5. Draft the host-model answer from the returned MOS context.
6. Write that exact answer to a UTF-8 file.
7. Execute `MOS_COMMIT_TURN.py` before displaying the answer.
8. Report state persistence only when the Git publication succeeded.

The host model is not called by MOS through an API. The host orchestrator calls
MOS, generates the answer itself, and commits the answer back into MOS.
