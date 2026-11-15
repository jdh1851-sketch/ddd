from pathlib import Path
import ast
import numpy as np
from PIL import Image
from spear_observation import NativeMapReader,canonical_label
from spear_reading_rules import coordinate_consensus,pattern_pair


def test_exact_readings_reject_conflicts():
    assert coordinate_consensus(['X-1.88,Y162.35','X-1.88,Y162.35'])==(-1.88,162.35)
    assert coordinate_consensus(['X-1.88,Y162.35','X1.88,Y162.35']) is None
    assert pattern_pair(['투창병·불']*2,['정답·독']*2) is None


def test_duplicate_targets_are_ambiguous():
    rows=[dict(prefix='정답',element='불',box=b) for b in [(10,10,40,25),(100,100,130,115)]]
    assert canonical_label(rows,'정답') is None


def test_native_crop_ocr_and_no_stale_reuse():
    image=np.array(Image.open(Path(__file__).parent/'fixtures/native_fire.png').convert('RGB'))
    reader=NativeMapReader()
    result=reader.read(image,budget_seconds=20)
    assert result['coordinates']==(-1.88,162.35)
    assert result['element']=='불' and result['star']
    assert not result['movement_allowed'] and not result['success_confirmed']
    assert result['target_world_coordinates'] is None
    blank=reader.read(np.zeros_like(image))
    assert blank['element'] is None and blank['coordinates'] is None and not blank['star']


def test_preview_dispatch_blocks_legacy_inputs():
    tree=ast.parse((Path(__file__).parents[1]/'kkullim_pc.py').read_text(encoding='utf-8'))
    app=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='App')
    methods={n.name:n for n in app.body if isinstance(n,ast.FunctionDef)}
    assert isinstance(methods['fishing_key'].body[0],ast.Return)
    # Both observation dispatches return before the unconditional input lock.
    statements=methods['step'].body
    assert ast.unparse(statements[2].test)=='self.is_packet()'
    assert isinstance(statements[2].body[-1],ast.Return)
    assert ast.unparse(statements[3]).startswith('self.stop(')
    assert isinstance(statements[4],ast.Return)
