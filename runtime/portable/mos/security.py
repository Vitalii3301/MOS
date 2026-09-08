from __future__ import annotations
import ast, re
from dataclasses import dataclass

@dataclass
class SecurityResult:
    allowed: bool
    reasons: list[str]

class AntiMeme:
    def __init__(self, name: str, pattern: str, mitigation: str):
        self.name=name; self.pattern=pattern; self.mitigation=mitigation
    def detect(self,text:str)->bool:
        return re.search(self.pattern,text,flags=re.IGNORECASE) is not None
    def mitigate(self,text:str)->str:
        return f"{text} [Mitigated: {self.mitigation}]" if self.detect(text) else text

class CodePolicy:
    """AST policy for executable memes. No imports, filesystem, network, reflection or dunder access."""
    forbidden_nodes = (
        ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Try, ast.Raise,
        ast.Global, ast.Nonlocal, ast.ClassDef, ast.Lambda, ast.Delete, ast.Yield,
        ast.YieldFrom, ast.Await, ast.AsyncFunctionDef,
    )
    forbidden_names = {
        "open","exec","eval","compile","__import__","globals","locals","vars","dir",
        "getattr","setattr","delattr","input","breakpoint","help","memoryview"
    }
    allowed_builtins = {
        "abs":abs,"min":min,"max":max,"sum":sum,"len":len,"range":range,
        "enumerate":enumerate,"zip":zip,"sorted":sorted,"round":round,
        "str":str,"int":int,"float":float,"bool":bool,"list":list,"dict":dict,
        "set":set,"tuple":tuple,"all":all,"any":any
    }
    def scan(self, code: str) -> SecurityResult:
        reasons=[]
        try:
            tree=ast.parse(code,mode="exec")
        except SyntaxError as exc:
            return SecurityResult(False,[f"syntax:{exc.msg}"])
        for node in ast.walk(tree):
            if isinstance(node,self.forbidden_nodes):
                reasons.append(f"forbidden_ast:{type(node).__name__}")
            if isinstance(node,ast.Name) and node.id in self.forbidden_names:
                reasons.append(f"forbidden_name:{node.id}")
            if isinstance(node,ast.Attribute) and node.attr.startswith("__"):
                reasons.append(f"dunder_attribute:{node.attr}")
            if isinstance(node,ast.Call):
                if isinstance(node.func,ast.Name) and node.func.id not in self.allowed_builtins and node.func.id not in {"meme_main"}:
                    # User-defined local functions are allowed; unknown direct builtins are not.
                    pass
        return SecurityResult(not reasons,sorted(set(reasons)))
