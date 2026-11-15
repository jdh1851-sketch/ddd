"""Synthetic correlated evidence; no user packets and no game input."""
import ast
import json
import math
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spear_packet_state import SpearPacketState, SHIELDS, POSITION_MARKER, SPEAR_SIGNATURE

BOSS, SELF, MARKER, FIRE = 100, 200, 300, 401


def action(actor, name, target, skill):
    text = name.encode('utf-16le'); n = len(text)
    body = bytearray(41 + n)
    struct.pack_into('<QI', body, 0, actor, n); body[12:12 + n] = text
    struct.pack_into('<QI', body, 29 + n, target, skill)
    return bytes(body)


def status(actor, name):
    text = name.encode('utf-16le'); body = bytearray(37 + len(text))
    struct.pack_into('<QI', body, 0, actor, len(text)); body[12:12 + len(text)] = text
    return bytes(body)


def boss_spawn():
    body = bytearray(1636); struct.pack_into('<Q', body, 0, BOSS)
    body[638:662] = SPEAR_SIGNATURE; body[1100:1108] = POSITION_MARKER
    struct.pack_into('<fff', body, 1108, 0, 163, math.pi)
    return bytes(body)


def shaman_spawn(entity, skill, x, y):
    body = bytearray(1554); struct.pack_into('<Q', body, 0, entity)
    struct.pack_into('<I', body, 622, skill); body[1064:1072] = POSITION_MARKER
    struct.pack_into('<fff', body, 1072, x, y, math.pi)
    return bytes(body)


def marker_spawn(owner=BOSS, target=SELF, entity=MARKER):
    body = bytearray(211)
    for offset, value in ((0, entity), (67, owner), (182, target)):
        struct.pack_into('<Q', body, offset, value)
    return bytes(body)


def marker_freeze(entity=MARKER, x=9, y=160, reference=2**64 - 1):
    body = bytearray(41); struct.pack_into('<QfffQ', body, 0, entity, x, y, math.pi, reference)
    return bytes(body)


def setup_state(confirm_shamans=True):
    state = SpearPacketState()
    feed = lambda typ, body, t: state.feed(typ, body, t, 'server', 0)
    feed(20051, boss_spawn(), 0)
    slots = [(10, 160), (10, 151), (10, 142), (-10, 142), (-10, 160), (-10, 151)]
    for i, (skill, (element, suffix)) in enumerate(SHIELDS.items()):
        entity = FIRE + i
        feed(20051, shaman_spawn(entity, skill, *slots[i]), .1)
        if confirm_shamans:feed(170064, action(entity, 'Abyss_Shield_' + suffix, SELF, skill), .2)
    # A boss-owned persistent buff. Duration bits are intentionally unknown;
    # the observer must not turn this into a countdown.
    feed(170069, struct.pack('<QQIfIQ', BOSS, 55, 1163329532, -1e10, 1, BOSS), .5)
    feed(170064, action(BOSS, 'Penetrate_Casting', SELF, 1791041054), 1)
    feed(20066, marker_spawn(), 1.1)
    return state


def advance(state, to):
    # Keep the data stream present through a long wind-up; this does not
    # manufacture an exit/impact signal.
    current = state.snapshot(now=0)['cast']['started_at']
    for t in range(math.ceil(current) + 1, int(to) + 1):
        state.feed(20132, b'', float(t), 'server')


def lock_and_release(state):
    advance(state, 7)
    state.feed(171128, struct.pack('<Qfff', MARKER, 9, 160, math.pi), 7, 'server')
    state.feed(171126, marker_freeze(), 7, 'server')
    state.feed(170064, action(BOSS, 'Penetrate', SELF, 1791041054), 8.5, 'server')


def impact(state, caster=BOSS, target=FIRE, kind=3, flag=1, t=8.8):
    state.feed(170068, struct.pack('<QIfQI', target, kind, 3, caster, flag), t, 'server')


def test_measured_fire_chain_requires_correlated_events_and_never_enables_input():
    state = setup_state(); lock_and_release(state)
    before = state.snapshot(now=8.5)
    assert before['departure_candidate'] and before['stage'] == 'released'
    assert not before['success_observed']
    impact(state)
    assert not state.snapshot(now=8.8)['success_observed']
    state.feed(170063, status(BOSS, 'Groggy'), 8.8, 'server')
    state.feed(170070, struct.pack('<QQ', BOSS, 55), 8.8, 'server')
    result = state.snapshot(now=8.8)
    assert result['success_observed'] and result['stage'] == 'success_observed'
    assert result['cast']['success_evidence']['boss_groggy_correlated']
    assert result['cast']['success_evidence']['matched_boss_self_buff_removed']
    assert result['target_shaman']['entity_id'] == FIRE
    assert all(not result[key] for key in ('self_identity_confirmed', 'game_input_allowed', 'departure_control_verified'))
    assert len([e for e in result['events'] if e['event'] == 'success_observed']) == 1


@pytest.mark.parametrize('alteration', ['wrong_owner', 'wrong_target', 'wrong_marker', 'reference_not_cleared', 'far_position', 'unconfirmed_shaman'])
def test_marker_or_position_alone_is_not_departure_proof(alteration):
    state = setup_state(confirm_shamans=alteration != 'unconfirmed_shaman')
    if alteration == 'wrong_owner':
        state.reset(); state = setup_state()
        # Replace the current cycle before introducing a different-owned marker.
        state.feed(170064, action(BOSS, 'Penetrate_Casting', SELF, 1791041054), 2, 'server')
        state.feed(20066, marker_spawn(owner=999), 2.1, 'server')
    if alteration == 'wrong_target':
        state.feed(170064, action(BOSS, 'Penetrate_Casting', SELF, 1791041054), 2, 'server')
        state.feed(20066, marker_spawn(target=999), 2.1, 'server')
    advance(state, 7)
    entity = 999 if alteration == 'wrong_marker' else MARKER
    x = 1000 if alteration == 'far_position' else 9
    state.feed(171128, struct.pack('<Qfff', entity, x, 160, math.pi), 7, 'server')
    body = marker_freeze(entity, x=x, reference=SELF if alteration == 'reference_not_cleared' else 2**64 - 1)
    state.feed(171126, body, 7, 'server')
    assert not state.snapshot(now=7)['departure_candidate']


@pytest.mark.parametrize('caster,target,kind,flag', [(999,FIRE,3,1), (BOSS,FIRE+1,3,1), (BOSS,FIRE,1,1), (BOSS,FIRE,3,0)])
def test_wrong_hit_or_unrelated_effect_never_confirms_success(caster,target,kind,flag):
    state = setup_state(); lock_and_release(state)
    impact(state,caster,target,kind,flag)
    state.feed(170063, status(BOSS, 'Groggy'), 8.8, 'server')
    state.feed(170070, struct.pack('<QQ', BOSS, 55), 8.8, 'server')
    assert not state.snapshot(now=8.8)['success_observed']


def test_unrelated_buff_removal_and_star_despawn_are_not_success():
    state = setup_state(); lock_and_release(state); impact(state)
    state.feed(170070, struct.pack('<QQ', BOSS, 999), 8.8, 'server')
    state.feed(20272, struct.pack('<Q', MARKER), 8.8, 'server')
    assert not state.snapshot(now=8.8)['success_observed']
    state.feed(170070, struct.pack('<QQ', BOSS, 55), 8.8, 'server')
    assert state.snapshot(now=8.8)['success_observed']


def test_unknown_attribute_revokes_previous_fire_and_does_not_map_by_order():
    state = setup_state(); lock_and_release(state)
    state.feed(170064, action(BOSS, 'Penetrate_Casting', SELF, 324458883), 9, 'server')
    result = state.snapshot(now=9)
    assert result['stage'] == 'unsupported_cast' and result['element'] is None
    assert not result['departure_candidate'] and not result['success_observed']
    impact(state,t=9.1); state.feed(170063,status(BOSS,'Groggy'),9.1,'server')
    assert not state.snapshot(now=9.1)['success_observed']


def test_connection_generation_or_silence_cannot_revive_an_old_cast():
    state = setup_state()
    assert state.snapshot(now=5)['stage'] == 'stale'
    assert not state.snapshot(now=5)['phase_active']
    state.feed(171126, marker_freeze(), 6, 'server')
    assert state.snapshot(now=6)['cast'] is None
    state = setup_state()
    state.feed(171126, marker_freeze(), 2, 'server', generation=1)
    assert state.snapshot(now=2)['cast'] is None
    state.reset('server')
    assert state.snapshot(now=2)['stage'] == 'waiting'


def test_duplicate_and_reordered_correlated_evidence_is_idempotent():
    state = setup_state(); lock_and_release(state)
    groggy = status(BOSS,'Groggy')
    state.feed(170063, groggy, 8.8, 'server')
    state.feed(170063, groggy, 8.8, 'server')
    impact(state)
    impact(state)
    result = state.snapshot(now=8.8)
    assert result['success_observed']
    assert sum(e['event']=='success_observed' for e in result['events'])==1


@pytest.mark.parametrize('body', [b'', action(BOSS,'Penetrate_Casting',SELF,1791041054)[:-1], struct.pack('<QI', BOSS, 513)+b'x'*554])
def test_malformed_action_cannot_start_or_replace_a_valid_phase(body):
    state = SpearPacketState(); state.feed(20051,boss_spawn(),0,'server')
    state.feed(170064,body,1,'server')
    result = state.snapshot(now=1)
    assert result['stage']=='waiting' and result['cast'] is None
    assert result['malformed_messages']>=1


def test_death_before_linked_impact_is_unverified_end():
    state = setup_state(); lock_and_release(state)
    state.feed(170063,status(BOSS,'Die_Blow'),8.6,'server')
    impact(state);state.feed(170063,status(BOSS,'Groggy'),8.8,'server')
    result=state.snapshot(now=8.8)
    assert not result['success_observed'] and result['stage']=='unverified_end'


def test_events_and_position_caches_are_bounded_without_ownership_inference():
    state=setup_state()
    for i in range(400):
        state.update_position(dict(entity_id=1000+i,x=1,y=2,timestamp=.5,stream_id='server'))
        state.feed(170064,action(BOSS,'Penetrate_Casting',SELF,1791041054),2+i*.01,'server')
    result=state.snapshot(now=6)
    assert len(result['events'])<=128 and not result['self_identity_confirmed']
    assert not result['game_input_allowed']
    json.dumps(result,allow_nan=False)
    tree=ast.parse((Path(__file__).parents[1]/'spear_packet_state.py').read_text(encoding='utf-8'))
    forbidden={'SendInput','keybd_event','mouse_event','SetCursorPos','pcap_inject','pcap_sendpacket','sendto'}
    assert not any(isinstance(n,ast.Attribute) and n.attr in forbidden for n in ast.walk(tree))
