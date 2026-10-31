import sys
import io
import base64
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fishing import FishingController, Signal, classify, result_kind, gauge_level, idle_score

@pytest.mark.parametrize('text,key', [
    ('묵직한 느낌이 전해진다!', 'heavy'), ('예감이 좋다!', 'lucky'),
    ('찌가 흔들렸다.', 'float'), ('입질이 왔다.', 'bite'),
    ('반응이 왔다.', 'response'), ('뭔가 걸렸다...', 'caught'),
    ('찌가\n흔들렸다!', 'float'), ('씨가 은들렸다.', 'float'),
    ('피가 흔들렸다.', 'float'), ('찌가 은들렸다.', 'float'),
    ('', None), ('낚시터가 풍족합니다', None), ('알 수 없는 문구', None)])
def test_messages(text,key):
    assert classify(text)==key

def test_spawn_animation_is_not_rebound():
    e=FishingController()
    for i,v in enumerate([.12,.3,.5,.9,1.,.97,.9,.8]):
        assert e.update(Signal(v,'float'),i*.05) is None

def test_latched_message_one_input_and_confirmed_count():
    e=FishingController()
    for i,v in enumerate([1.,.8,.5,.2,.23,.24]):
        a=e.update(Signal(v,'float' if i==0 else None),i*.05)
    assert a=='reel'
    e.sent(a,.3)
    assert e.catches==0
    for i in range(5): assert e.update(Signal(.5,'float'),.4+i*.05) is None
    assert e.update(Signal(result='success'),1) is None
    assert e.catches==1
    e.update(Signal(result='success'),1.1)
    assert e.catches==1

@pytest.mark.parametrize('phrase',['response','caught'])
def test_low_quality_cancel_requires_confirmation(phrase):
    e=FishingController()
    assert e.update(Signal(.8,phrase),0)=='cancel'
    e.sent('cancel',0)
    assert e.cancels==0
    for i in range(3): e.update(Signal(idle=True),1+i*.1)
    assert e.cancels==1

def test_unknown_message_never_reels():
    e=FishingController()
    for i,v in enumerate([1.,.8,.5,.2,.3,.4]):
        assert e.update(Signal(v),i*.1) is None

def test_failed_input_stops_instead_of_repeating():
    e=FishingController();e.sent('cancel',0)
    assert e.update(Signal(.5,'caught'),13)=='stop'
    assert e.cancels==0

def test_short_gauge_dropout_tolerated():
    e=FishingController();e.update(Signal(1.,'float'),0)
    for i in range(7):assert e.update(Signal(),i*.05) is None
    assert e.update(Signal(.8),.5) is None

def test_focus_loss_does_not_create_rebound():
    e=FishingController();e.update(Signal(1.,'float'),0);e.update(Signal(.2),1)
    e.interrupted()
    for i in range(4): assert e.update(Signal(.5,'float'),2+i*.05) is None

def test_start_requires_stable_idle_button():
    e=FishingController()
    assert e.update(Signal(),0) is None
    assert e.update(Signal(idle=True),.1) is None
    assert e.update(Signal(idle=True),.2) is None
    assert e.update(Signal(idle=True),.3)=='cast'
    e.sent('cast',.3)
    assert e.update(Signal(idle=True),.4) is None

def test_result_parsing():
    assert result_kind('싱싱한 녀석을 낚았다.')=='success'
    assert result_kind('물고기를 놓쳤다.')=='failed'
    assert result_kind('찌가 흔들렸다.') is None

def test_flat_template_is_rejected():
    flat=np.zeros((100,100,3),np.uint8)
    assert idle_score(flat,flat)==0

@pytest.mark.parametrize('index,earliest,latest', [(1,20.2,20.9),(2,28.5,29.3)])
def test_uploaded_video_gauge_rebound(index,earliest,latest):
    # Cropped gauge frames only, preserving actual variable-frame-rate timestamps.
    fixture=Path(__file__).parent/'fixtures/fishing_gauges.npz'
    source=fixture if fixture.exists() else io.BytesIO(base64.b64decode(fixture.with_suffix('.npz.b64').read_text()))
    data=np.load(source)
    e=FishingController();actions=[]
    for mask,t in zip(data[f'masks{index}'],data[f'times{index}']):
        patch=np.zeros((*mask.shape,3),np.uint8);patch[mask]=(0,255,0)
        a=e.update(Signal(gauge_level(patch),'float'),float(t))
        if a:
            actions.append((float(t),a));break
    assert len(actions)==1
    assert actions[0][1]=='reel'
    assert earliest<=actions[0][0]<=latest
