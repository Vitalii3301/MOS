from __future__ import annotations
import re,time
from typing import Any
from .storage import SQLiteStore

class StateController:
    transitions={
      'neutral':{'curious':['why','how','почему','как','зачем'],'frustrated':['fail','error','problem','ошиб','проблем','не могу'],'motivated':['plan','next','strategy','план','следующ','стратег']},
      'curious':{'neutral':['ok','понятно','ясно'],'frustrated':['unclear','confused','непонят','запут']},
      'frustrated':{'neutral':['resolved','clear','решено','понятно'],'motivated':['try','again','попроб','снова']},
      'motivated':{'neutral':['done','complete','готово','заверш'],'frustrated':['failed','fail','провал','не удалось']},
    }
    def __init__(self,store:SQLiteStore):self.store=store

    def recover(self, *, max_elapsed: float = 60.0, now: float | None = None) -> dict[str,Any]:
        """Recover bounded energy using the R2 debt-aware rule.

        Recovery is deliberately capped so a runtime that was offline for hours
        does not wake up with an artificial full recharge. Debt can suppress
        recovery by at most 50%, matching the working UMA/R2 design.
        """
        state=self.store.all_state();now=time.time() if now is None else float(now)
        last=float(state.get('last_tick',now))
        elapsed=max(0.0,min(float(max_elapsed),now-last))
        debt=float(state.get('debt',0.0))
        debt_factor=1.0-min(debt/10.0,0.5)
        rate=max(0.0,float(state.get('energy_recovery_rate',0.7)))
        energy=min(100.0,float(state.get('energy',100.0))+elapsed*rate*debt_factor)
        self.store.set_state('energy',round(energy,4));self.store.set_state('last_tick',now)
        return self.store.all_state()

    def pre_cycle(self,text:str,strategy_count:int)->dict[str,Any]:
        # Recover before charging this cycle. The elapsed window is bounded so
        # restart/offline time cannot silently erase accumulated cognitive cost.
        state=self.recover(max_elapsed=60.0);low=text.lower()
        ctx=dict(state.get('context',{}))
        if re.search(r'[А-Яа-яЁё]',text):ctx['language']='ru'
        if any(k in low for k in ('tech','ai','технолог','квант')):ctx['audience']='tech_savvy';ctx['tone']='technical'
        emotion=str(state.get('emotion','neutral'))
        for target,keys in self.transitions.get(emotion,{}).items():
            if any(k in low for k in keys):emotion=target;break
        cost=min(12.0,1.0+len(text)/900+0.55*strategy_count+0.15*float(state.get('debt',0))+0.1*float(state.get('pain',0)))
        energy=max(0.0,float(state.get('energy',100))-cost)
        self.store.set_state('context',ctx);self.store.set_state('emotion',emotion);self.store.set_state('energy',round(energy,4))
        return self.store.all_state()
    def outcome(self,success:bool|None,revision:bool=False):
        s=self.store.all_state();debt=float(s.get('debt',0));pain=float(s.get('pain',0));conf=float(s.get('confidence',.75))
        if success is True:
            debt=max(0.0,debt-.5);pain=max(0.0,pain-.25);conf=min(1.0,conf+.03)
        elif success is False or revision:
            debt=min(10.0,debt+.7);pain=min(10.0,pain+1.0);conf=max(0.0,conf-.06)
        self.store.set_state('debt',round(debt,4));self.store.set_state('pain',round(pain,4));self.store.set_state('confidence',round(conf,4))
