from __future__ import annotations
import json, shlex, textwrap
from .memes import MemeEngine

class MPLInterpreter:
    """Memetic Programming Language for the full versioned meme lifecycle.

    Commands:
      meme NAME { ... }          create code
      update NAME { ... }        replace with a new full-code version
      mutate NAME { ... }        append an explicit mutation
      execute NAME [JSON]        execute
      evaluate NAME JSON_CASES   score current version
      evolve NAME JSON_OBJECT    evaluate candidate full-code variants
      diff NAME v1 v2
      rollback NAME vN
      status NAME
      list / ontology
    """
    def __init__(self, memes: MemeEngine):
        self.memes = memes

    @staticmethod
    def _block(raw: list[str], i: int, header: str) -> tuple[str, list[str], int]:
        parts = header[:-1].strip().split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid block header: {header}")
        name = parts[1].strip()
        body: list[str] = []
        i += 1
        while i < len(raw) and raw[i].strip() != "}":
            body.append(raw[i])
            i += 1
        if i >= len(raw):
            raise ValueError(f"Unclosed block: {name}")
        return name, body, i + 1

    def execute(self, script: str) -> list[dict]:
        raw = script.splitlines()
        out: list[dict] = []
        i = 0
        while i < len(raw):
            stripped = raw[i].strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            for command in ("meme", "update", "mutate"):
                if stripped.startswith(command + " ") and stripped.endswith("{"):
                    name, body, i = self._block(raw, i, stripped)
                    code = textwrap.dedent("\n".join(body)).strip("\n")
                    if command == "meme":
                        mid = self.memes.create(name, code)
                        out.append({"op": "meme", "name": name, "meme_id": mid})
                    elif command == "update":
                        version = self.memes.update(name, code, "MPL full-code update", parents=[name])
                        out.append({"op": "update", "name": name, "version": version})
                    else:
                        version = self.memes.mutate(name, code, "MPL explicit mutation")
                        out.append({"op": "mutate", "name": name, "version": version})
                    break
            else:
                if stripped.startswith("evaluate "):
                    _, name, payload = stripped.split(None, 2)
                    cases = json.loads(payload)
                    if not isinstance(cases, list):
                        raise ValueError("evaluate expects a JSON list of cases")
                    out.append({"op": "evaluate", "name": name, "fitness": self.memes.evaluate(name, cases)})
                    i += 1
                    continue
                if stripped.startswith("evolve "):
                    _, name, payload = stripped.split(None, 2)
                    spec = json.loads(payload)
                    if not isinstance(spec, dict) or not isinstance(spec.get("candidates"), list) or not isinstance(spec.get("cases"), list):
                        raise ValueError("evolve expects JSON object with candidates[] and cases[]")
                    out.append({"op": "evolve", "name": name, "result": self.memes.evolve(name, spec["candidates"], spec["cases"])})
                    i += 1
                    continue

                parts = shlex.split(stripped)
                cmd = parts[0]
                if cmd == "execute":
                    inp = json.loads(parts[2]) if len(parts) > 2 else None
                    out.append({"op": "execute", "name": parts[1], "result": self.memes.execute(parts[1], inp)})
                elif cmd == "diff":
                    out.append({"op": "diff", "name": parts[1], "diff": self.memes.diff(parts[1], int(parts[2].lstrip("v")), int(parts[3].lstrip("v")))})
                elif cmd == "rollback":
                    out.append({"op": "rollback", "name": parts[1], "ok": self.memes.rollback(parts[1], int(parts[2].lstrip("v")))})
                elif cmd == "status":
                    out.append({"op": "status", "name": parts[1], "meme": self.memes.current(parts[1])})
                elif cmd in {"list", "ontology"}:
                    out.append({"op": cmd, "ontology": self.memes.ontology()})
                else:
                    raise ValueError(f"Unknown MPL command: {cmd}")
                i += 1
                continue
            # block branch already advanced i
        return out
