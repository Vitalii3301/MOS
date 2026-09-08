from __future__ import annotations
import threading,time
from .util import utcnow

class SelfObserver:
    def __init__(self,runtime):
        self.runtime=runtime;self.thread=None;self.stop_event=threading.Event()
        row=self.runtime.store.conn.execute("SELECT count(*) n FROM events WHERE event_type='self_observation'").fetchone()
        self.counter=int(row['n']) if row else 0
    def observe(self,reason:str='cycle')->dict:
        s=self.runtime.state();self.counter+=1
        insight={
          'reason':reason,'energy':float(s.get('energy',0)),'confidence':float(s.get('confidence',0)),
          'debt':float(s.get('debt',0)),'pain':float(s.get('pain',0)),'emotion':s.get('emotion'),
          'cycle_count':int(s.get('cycle_count',0)),'timestamp':utcnow()
        }
        self.runtime.store.append_event(f"OBS-{self.counter}-{int(time.time()*1000)}",'self_observation',insight)
        if self.counter%5==0 or reason=='manual':
            statement=(f"Self-observation: energy={insight['energy']:.1f}, confidence={insight['confidence']:.2f}, "
                       f"debt={insight['debt']:.1f}, pain={insight['pain']:.1f}, emotion={insight['emotion']}.")
            self.runtime.meta.remember('self_observation',statement,importance=.65,confidence=1.0,
                                       verification_status='verified',evidence_refs=['runtime_state'])
        return insight
    def _loop(self,interval:float):
        while not self.stop_event.wait(interval):
            try:
                self.runtime.state_controller.recover(max_elapsed=max(1.0,interval*2.0));self.observe('background');self.runtime.memory.decay();self.runtime.strategies.learn();self.runtime.strategies.evolve(max_new=1)
            except Exception as exc:
                self.runtime.store.append_event(f"OBS-ERR-{int(time.time()*1000)}",'observer_error',{'error':str(exc)})
    def start(self,interval:float=10.0):
        if self.thread and self.thread.is_alive():return False
        self.stop_event.clear();self.thread=threading.Thread(target=self._loop,args=(interval,),daemon=True,name='mos-self-observer');self.thread.start();return True
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2.0)
    @property
    def running(self):return bool(self.thread and self.thread.is_alive())
