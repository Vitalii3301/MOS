from __future__ import annotations
import re
from typing import Any

class MandatoryCheckRouter:
    def route(self,context:dict[str,Any],user_input:str)->list[dict[str,Any]]:
        checks=[]
        active=[x for x in context["items"] if x["kind"]=="self_mandatory_check"]
        if not active:return checks
        nums=re.findall(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?",user_input)
        if len(nums)>=2:
            checks.append({"check_id":"numeric_comparison","status":"executed","values":[float(x.replace(",",".")) for x in nums[:8]],
                           "reason":"active mandatory check + numeric evidence present"})
        if any(k in user_input.lower() for k in ("source","источник","proof","доказ","verify","провер")):
            checks.append({"check_id":"evidence_required","status":"executed","reason":"claim requests verification"})
        return checks
