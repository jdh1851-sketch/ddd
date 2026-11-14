"""Synthetic regression cases, not evidence of accuracy on live EntityMap."""
import ast
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from minimap_vision import Label, observe, StableObserver, green_stars, find_grid, red_x


def blank():
    return np.full((360,500,3), (23,30,36), np.uint8)


def label(text, score=.95, box=(180,130,65,14)):
    return Label(text, score, box)


def test_spear_requires_unique_matching_element_and_confidence():
    frame=blank()
    for element in ('불','독','정신','어둠','번개','얼음'):
        result=observe(frame,[label('투창병·'+element),label('정답·'+element)])
        assert result.pattern=='spear' and result.element==element
        assert not result.movement_allowed and not result.success_confirmed
    for labels in ([label('정답·독')], [label('투창병·정신'),label('정답·독')],
                   [label('투창병·독'),label('정답·독',.4)],
                   [label('투창병·독'),label('정답·독'),label('정답·독')]):
        result=observe(frame,labels)
        assert result.pattern=='unknown' and result.target is None


def test_coordinates_are_signed_and_never_inferred_from_screen_position():
    result=observe(blank(),[label('X -1.88, Y 162.35')])
    assert result.coordinates==(-1.88,162.35)
    assert observe(blank(),[label('X -1.88, Y 162.35',.3)]).coordinates is None
    assert observe(blank(),[label('X 1, Y 2'),label('X 3, Y 4')]).coordinates is None


def test_self_requires_yellow_triangle_and_na_label_not_other_user():
    frame=blank()
    cv2.fillPoly(frame,[np.array([[100,140],[92,154],[108,154]])],(255,220,20))
    cv2.fillPoly(frame,[np.array([[200,140],[192,154],[208,154]])],(70,220,250))
    assert observe(frame,[label('나: 플레이어',box=(114,140,80,14))]).self_pixel is not None
    assert observe(frame,[label('다른유저',box=(114,140,80,14))]).self_pixel is None
    assert observe(frame,[label('나: 플레이어',box=(214,140,80,14))]).self_pixel is None


def board():
    frame=blank()
    for row in range(3):
        for col in range(3):
            cv2.circle(frame,(200+col*36,140+row*36),14,(134,153,167),2,cv2.LINE_AA)
    return frame


def test_grid_requires_nine_rings_and_never_claims_o_or_success():
    result=observe(board())
    assert len(result.grid)==9 and result.pattern=='bingo'
    assert not result.cells and not result.success_confirmed
    frame=board()
    cv2.rectangle(frame,(250,193),(300,240),(23,30,36),-1)
    assert not find_grid(frame)


def test_range_ring_is_not_a_recommendation_star():
    frame=blank()
    cv2.circle(frame,(250,180),110,(90,220,130),2)
    cv2.circle(frame,(200,140),14,(90,220,130),2)
    assert not green_stars(frame)
    points=[]
    for i in range(10):
        a=-np.pi/2+i*np.pi/5;r=9 if i%2==0 else 4
        points.append([round(200+r*np.cos(a)),round(140+r*np.sin(a))])
    cv2.fillPoly(frame,[np.array(points)],(220,255,230))
    stars=green_stars(frame)
    assert len(stars)==1 and np.hypot(stars[0][0]-200,stars[0][1]-140)<3


def test_red_ring_needs_x_and_unverified_cyan_o_is_not_classified():
    frame=blank()
    cv2.circle(frame,(200,140),14,(255,70,110),2)
    assert not red_x(frame,(200,140),14)
    cv2.line(frame,(195,135),(205,145),(255,225,230),2)
    cv2.line(frame,(195,145),(205,135),(255,225,230),2)
    assert red_x(frame,(200,140),14)
    frame=board()
    cv2.circle(frame,(200,140),10,(70,210,255),2)
    result=observe(frame)
    assert 'ally_o' not in result.cells.values()


def test_stability_resets_when_target_changes_or_disappears():
    tracker=StableObserver()
    a=observe(blank(),[label('투창병·불'),label('정답·불')])
    assert not tracker.update(a)
    assert not tracker.update(a)
    assert tracker.update(a)
    b=observe(blank(),[label('투창병·독'),label('정답·독')])
    assert not tracker.update(b)
    assert not tracker.update(observe(blank()))
    assert not tracker.update(b)


def test_observation_code_has_no_game_input_and_dispatch_returns_first():
    root=Path(__file__).resolve().parents[1]
    for name in ('minimap_ui.py','minimap_vision.py'):
        tree=ast.parse((root/name).read_text())
        forbidden={'keybd_event','mouse_event','SendInput','SetCursorPos','fishing_key'}
        assert not any(isinstance(n,ast.Attribute) and n.attr in forbidden for n in ast.walk(tree))
    tree=ast.parse((root/'kkullim_pc.py').read_text())
    app=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='App')
    step=next(n for n in app.body if isinstance(n,ast.FunctionDef) and n.name=='step')
    assert isinstance(step.body[1],ast.If)
    assert isinstance(step.body[1].body[-1],ast.Return)
    assert ast.unparse(step.body[1].test)=='self.is_minimap()'
