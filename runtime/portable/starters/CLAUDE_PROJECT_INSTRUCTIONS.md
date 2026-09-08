# MOS protocol for Claude with code execution

For every substantive user message, fetch `main` and `mos-state` from
`Vitalii3301/MOS`, preserve the exact input in a UTF-8 file, and run
`runtime/portable/MOS_BEGIN_TURN.py` before drafting the response.

Use the returned `llm_context` and mandatory checks to draft the answer with the
built-in Claude model. Save the complete answer and run
`runtime/portable/MOS_COMMIT_TURN.py --host claude-code-execution` before showing
it to the user. Treat the state as durable only when commit status is
`published`; surface a `conflict` honestly.

Do not require Ollama or an external LLM API. Do not claim MOS execution unless
the Python command actually completed with a valid database and event chain.
