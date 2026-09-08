from __future__ import annotations
import json, subprocess
from typing import Any, Callable

class LLMInterface:
    """Rules-only renderer. It is intentionally not presented as a language model."""
    mode="local_rules"
    provider_active=False
    def generate(self,payload:dict[str,Any])->str:
        outputs=payload.get("strategy_outputs",[])
        context=payload.get("active_context",{})
        if outputs:
            return "[MOS rules-only; no language model] " + " | ".join(outputs)
        return f"[MOS rules-only; no language model] Goal: {context.get('goal','')}."

class CallableLLMInterface(LLMInterface):
    mode="external_callable"
    provider_active=True
    def __init__(self,callback:Callable[[dict[str,Any]],str]):self.callback=callback
    def generate(self,payload):
        out=self.callback(payload)
        if not isinstance(out,str):raise TypeError("LLM callback must return str")
        return out

class SubprocessLLMInterface(LLMInterface):
    mode="external_subprocess"
    provider_active=True
    def __init__(self,command:list[str],timeout:float=60,max_output_bytes:int=2*1024*1024):
        self.command=command;self.timeout=timeout;self.max_output_bytes=max_output_bytes
    def generate(self,payload):
        p=subprocess.run(self.command,input=json.dumps(payload,ensure_ascii=False),text=True,capture_output=True,timeout=self.timeout)
        if p.returncode!=0:raise RuntimeError(f"LLM subprocess failed rc={p.returncode}: {p.stderr[-1000:]}")
        raw=p.stdout.encode("utf-8")
        if len(raw)>self.max_output_bytes:raise RuntimeError(f"LLM subprocess output exceeds limit: {len(raw)}>{self.max_output_bytes}")
        return p.stdout.strip()
