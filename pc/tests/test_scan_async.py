import sys
from pathlib import Path
from threading import Event
from time import monotonic,sleep
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scan_async import LatestScan

def test_busy_scan_does_not_block_ui_poll_or_reset():
    worker=LatestScan();release=Event();entered=Event()
    def slow():
        entered.set();release.wait(2);return 'old game frame'
    try:
        assert worker.submit(slow)
        assert entered.wait(1)
        start=monotonic()
        for _ in range(100): assert worker.poll() is None
        worker.reset()
        assert monotonic()-start<.1
        release.set()
        deadline=monotonic()+1
        while worker.future and not worker.future[0].done() and monotonic()<deadline:sleep(.005)
        assert worker.poll() is None
        assert worker.submit(lambda:'new game frame')
        deadline=monotonic()+1
        while not worker.future[0].done() and monotonic()<deadline:sleep(.005)
        assert worker.poll()=='new game frame'
    finally:
        release.set();worker.close()

def test_busy_worker_does_not_queue_captures():
    worker=LatestScan();release=Event()
    try:
        assert worker.submit(lambda:release.wait(1))
        assert not worker.submit(lambda:1)
    finally:
        release.set();worker.close()
