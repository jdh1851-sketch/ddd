"""Single background vision task with generation-based stale-result rejection."""
from concurrent.futures import ThreadPoolExecutor
import time

class LatestScan:
    def __init__(self):
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='game-vision')
        self.future=None
        self.generation=0

    def reset(self):
        self.generation+=1
        if self.future: self.future[0].cancel()

    def submit(self,fn,*args):
        if self.future is not None: return False
        self.future=(self.pool.submit(fn,*args),self.generation,time.monotonic())
        return True

    def poll(self,max_age=3.):
        if self.future is None or not self.future[0].done(): return None
        job,generation,started=self.future
        self.future=None
        if generation!=self.generation or job.cancelled(): return None
        result=job.result()
        return result if time.monotonic()-started<=max_age else None

    def close(self):
        self.reset()
        self.pool.shutdown(wait=False,cancel_futures=True)
