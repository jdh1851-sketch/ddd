"""Read-only spear-mechanic observations for the measured 2026-11-15 profile.

Wire observations are deliberately separate from permission to control a game.
Only the observed Fire cast is classified. Time windows below correlate
received evidence; none schedules movement or declares success on a timer.
"""
from collections import deque
import hashlib
import json
import math
import struct
from threading import RLock
import time


POSITION_MARKER = bytes.fromhex('18271c28c3b6114d')
SPEAR_SKILLS = (1791041054, 324458883, 948487208, 1324307937, 597598050, 1369085966)
SPEAR_SIGNATURE = struct.pack('<6I', *SPEAR_SKILLS)
SHIELDS = {
    1830405861: ('불', 'Fire'), 1274302744: ('얼음', 'Ice'),
    657414607: ('번개', 'Lightning'), 1460758020: ('어둠', 'Dark'),
    1378134919: ('독', 'Poison'), 1714131206: ('정신', 'Soul'),
}
FIRE_SKILL = 1791041054
BOSS_SHIELD = 1163329532
PAIR_WINDOW = .25
RELEASE_IMPACT_WINDOW = 2.
STREAM_FRESHNESS = 3.


def _finite(*values):
    return all(math.isfinite(float(v)) and abs(float(v)) < 1e6 for v in values)


def _named(body, overhead):
    if len(body) < overhead: return None
    actor, length = struct.unpack_from('<QI', body)
    if not actor or length > 512 or length % 2 or len(body) != overhead + length:
        return None
    try: name = body[12:12 + length].decode('utf-16le', errors='strict')
    except UnicodeError: return None
    if len(name) > 256 or any(ord(c) < 32 for c in name): return None
    return actor, length, name


class SpearPacketState:
    def __init__(self):
        self.lock = RLock()
        self.scopes = {}
        self.events = deque(maxlen=128)
        self.malformed = 0
        self.last_time = None
        self.fed_at = None

    def _event(self, scope, name, timestamp, **fields):
        self.events.append(dict(event=name, timestamp=timestamp,
            stream_id=scope['stream_id'], generation=scope['generation'], **fields))

    def reset(self, stream_id=None):
        with self.lock:
            if stream_id is None:
                self.scopes.clear()
            else:
                self.scopes = {k: v for k, v in self.scopes.items() if k[0] != str(stream_id)}
            self.events.append(dict(event='stream_reset', stream_id=stream_id,
                                    timestamp=self.last_time))

    def _scope(self, stream_id, generation, timestamp):
        stream_id = str(stream_id); generation = int(generation)
        key = stream_id, generation
        if key in self.scopes and timestamp - self.scopes[key]['last_seen'] > STREAM_FRESHNESS:
            # Stream silence loses authority; a later message cannot revive an
            # old cast. This is freshness revocation, never a game-stage timer.
            self._event(self.scopes[key], 'stream_stale', timestamp)
            del self.scopes[key]
        if key not in self.scopes:
            # A generation change never inherits actors, roles or an active cast.
            self.scopes = {k: v for k, v in self.scopes.items() if k[0] != stream_id}
            if len(self.scopes) >= 32:
                oldest = min(self.scopes, key=lambda k: self.scopes[k]['last_seen'])
                del self.scopes[oldest]
            self.scopes[key] = dict(stream_id=stream_id, generation=generation,
                last_seen=timestamp, bosses={}, shamans={}, shield_actions={},
                positions={}, buffs={}, cast=None, statuses={}, breaks=[], removals=[],
                markers={}, seen=set(), seen_order=deque())
        scope = self.scopes[key]
        scope['last_seen'] = max(timestamp, scope['last_seen'])
        return scope

    def update_position(self, sample):
        """Position evidence does not establish who owns the character."""
        try:
            timestamp = float(sample['timestamp']); entity = int(sample['entity_id'])
            x, y = float(sample['x']), float(sample['y'])
            if not entity or not math.isfinite(timestamp) or not _finite(x, y): return
            stream, generation = sample['stream_id'], sample.get('generation', 0)
        except (KeyError, ValueError, TypeError): return
        with self.lock:
            scope = self._scope(stream, generation, timestamp)
            previous = scope['positions'].get(entity)
            if previous is None or timestamp >= previous['timestamp']:
                scope['positions'][entity] = dict(entity_id=entity, x=x, y=y, timestamp=timestamp)
            if len(scope['positions']) > 256:
                oldest = min(scope['positions'], key=lambda k: scope['positions'][k]['timestamp'])
                del scope['positions'][oldest]

    def feed(self, typ, body, timestamp, stream_id, generation=0):
        try:
            timestamp = float(timestamp); body = bytes(body); typ = int(typ)
            if not math.isfinite(timestamp) or len(body) > 256 * 1024: return
        except (ValueError, TypeError): return
        with self.lock:
            scope = self._scope(stream_id, generation, timestamp)
            self.last_time = timestamp if self.last_time is None else max(self.last_time, timestamp)
            self.fed_at = time.monotonic()
            key = typ, timestamp, hashlib.blake2s(body, digest_size=8).digest()
            if key in scope['seen']: return
            scope['seen'].add(key); scope['seen_order'].append(key)
            if len(scope['seen_order']) > 256: scope['seen'].discard(scope['seen_order'].popleft())
            try:
                self._feed(scope, typ, body, timestamp)
                self._departure(scope)
                self._success(scope)
            except (ValueError, struct.error, UnicodeError, OverflowError):
                self.malformed += 1

    def _feed(self, s, typ, b, t):
        if typ == 20051:
            if len(b) == 1636 and b[638:662] == SPEAR_SIGNATURE and b[1100:1108] == POSITION_MARKER:
                entity = struct.unpack_from('<Q', b)[0]
                x, y, heading = struct.unpack_from('<fff', b, 1108)
                if entity and _finite(x, y, heading):
                    s['bosses'][entity] = dict(entity_id=entity, x=x, y=y,
                        evidence='six_skill_ids_and_position_marker', timestamp=t)
                    self._event(s, 'boss_profile', t, entity_id=entity)
            elif len(b) == 1554 and b[1064:1072] == POSITION_MARKER:
                skill = struct.unpack_from('<I', b, 622)[0]
                if skill not in SHIELDS: return
                entity = struct.unpack_from('<Q', b)[0]
                x, y, heading = struct.unpack_from('<fff', b, 1072)
                if not entity or not _finite(x, y, heading): return
                element, suffix = SHIELDS[skill]
                s['shamans'][entity] = dict(entity_id=entity, x=x, y=y,
                    element=element, skill_id=skill, suffix=suffix,
                    role_confirmed=s['shield_actions'].get(entity) == skill, timestamp=t,
                    dead=False, evidence='spawn_profile_with_shield_skill')
                self._event(s, 'shaman_profile', t, entity_id=entity, element=element)
            # Strict profile matching; other spawn sizes remain unknown.
            if len(s['bosses']) > 32: s['bosses'].pop(next(iter(s['bosses'])))
            if len(s['shamans']) > 64: s['shamans'].pop(next(iter(s['shamans'])))
            return
        if typ == 170064:
            parsed = _named(b, 41)
            if parsed is None:
                self.malformed += 1; return
            actor, n, name = parsed
            target = struct.unpack_from('<Q', b, 29 + n)[0]
            skill = struct.unpack_from('<I', b, 37 + n)[0]
            if skill in SHIELDS and name == 'Abyss_Shield_' + SHIELDS[skill][1]:
                s['shield_actions'][actor] = skill
                if len(s['shield_actions']) > 64: s['shield_actions'].pop(next(iter(s['shield_actions'])))
                npc = s['shamans'].get(actor)
                if npc and npc['skill_id'] == skill:
                    npc['role_confirmed'] = True
                    self._event(s, 'shaman_role', t, entity_id=actor, element=npc['element'])
                return
            if actor not in s['bosses'] or name not in ('Penetrate_Casting', 'Penetrate'):
                return
            if name == 'Penetrate_Casting':
                old = s['cast']
                if old and t < old['started_at']: return
                verified = skill == FIRE_SKILL
                shield = next((instance for (entity, instance), buff in reversed(list(s['buffs'].items()))
                    if entity == actor and buff['skill_id'] == BOSS_SHIELD and buff['caster'] == actor), None)
                s['cast'] = dict(boss_id=actor, target_id=target, skill_id=skill,
                    element='불' if verified else None, started_at=t, released_at=None,
                    stage='casting' if verified else 'unsupported_cast',
                    shield_instance=shield, success=False, departure_candidate=False,
                    marker_id=None, failure=None, boss_dead=False)
                self._event(s, 'cast_started' if verified else 'unsupported_cast', t,
                    actor=actor, target=target, skill_id=skill, element=s['cast']['element'])
            else:
                cast = s['cast']
                if not cast or cast['failure'] or (actor, target, skill) != (
                        cast['boss_id'], cast['target_id'], cast['skill_id']): return
                if t < cast['started_at'] or not cast['element']: return
                cast['released_at'] = t
                if not cast['success']: cast['stage'] = 'released'
                self._event(s, 'cast_released', t, actor=actor, target=target, skill_id=skill)
            return
        if typ == 20066 and len(b) == 211:
            cast = s['cast']
            if not cast or not cast['element'] or cast['failure'] or cast['success']: return
            entity = struct.unpack_from('<Q', b)[0]
            owner = struct.unpack_from('<Q', b, 67)[0]
            target = struct.unpack_from('<Q', b, 182)[0]
            if not entity or t < cast['started_at'] or (owner, target) != (cast['boss_id'], cast['target_id']): return
            s['markers'][entity] = dict(entity_id=entity, owner_id=owner,
                target_id=target, created_at=t, position=None, frozen=None, dead=False)
            self._event(s, 'bound_marker', t, entity_id=entity, owner_id=owner, target_id=target)
            if len(s['markers']) > 64: s['markers'].pop(next(iter(s['markers'])))
            return
        if typ in (171128, 171126):
            if len(b) != (20 if typ == 171128 else 41):
                self.malformed += 1; return
            entity, x, y, heading = struct.unpack_from('<Qfff', b)
            marker = s['markers'].get(entity)
            if marker is None or marker['dead'] or not _finite(x, y, heading): return
            record = dict(x=x, y=y, heading=heading, timestamp=t)
            if typ == 171128: marker['position'] = record
            elif struct.unpack_from('<Q', b, 20)[0] == 2**64 - 1: marker['frozen'] = record
            return
        if typ == 170063:
            parsed = _named(b, 37)
            if parsed is None:
                self.malformed += 1; return
            entity, _, name = parsed
            if name == 'Groggy':
                s['statuses'][(entity, name)] = max(t, s['statuses'].get((entity, name), t))
                if len(s['statuses']) > 128: s['statuses'].pop(next(iter(s['statuses'])))
                if s['cast'] and entity in (s['cast']['boss_id'], self._target(s).get('entity_id')):
                    self._event(s, 'groggy', t, entity_id=entity)
            if name.startswith('Die'):
                if entity in s['shamans']: s['shamans'][entity]['dead'] = True
                cast = s['cast']
                if cast and entity == cast['boss_id']:
                    cast['boss_dead'] = True
                    if not cast['success']: cast['failure'] = 'boss_dead_without_impact'; cast['stage'] = 'unverified_end'
                elif cast and not cast['success'] and entity in (cast['target_id'], self._target(s).get('entity_id')):
                    cast['failure'] = 'target_lost'; cast['stage'] = 'unverified_end'
            return
        if typ == 170068:
            if len(b) != 28:
                self.malformed += 1; return
            entity, kind, duration, caster, flag = struct.unpack('<QIfQI', b)
            if not entity or not _finite(duration) or duration <= 0: return
            s['breaks'].append(dict(entity_id=entity, kind=kind, duration=duration,
                caster=caster, flag=flag, timestamp=t))
            s['breaks'] = s['breaks'][-64:]
            return
        if typ == 170069:
            if len(b) != 36:
                self.malformed += 1; return
            entity, instance, skill = struct.unpack_from('<QQI', b)
            caster = struct.unpack_from('<Q', b, 28)[0]
            if any(r['entity_id'] == entity and r['instance'] == instance and r['timestamp'] >= t
                   for r in s['removals']): return
            if s['buffs'].get((entity, instance), {}).get('timestamp', t) > t: return
            s['buffs'][(entity, instance)] = dict(skill_id=skill, caster=caster, timestamp=t)
            if len(s['buffs']) > 256: s['buffs'].pop(next(iter(s['buffs'])))
            cast = s['cast']
            if cast and not cast['failure'] and entity == caster == cast['boss_id'] and skill == BOSS_SHIELD:
                cast['shield_instance'] = instance
            return
        if typ == 170070:
            if len(b) != 16:
                self.malformed += 1; return
            entity, instance = struct.unpack('<QQ', b)
            if s['buffs'].get((entity, instance), {}).get('timestamp', t) > t: return
            buff = s['buffs'].pop((entity, instance), None)
            if buff and buff['skill_id'] == BOSS_SHIELD and buff['caster'] == entity:
                s['removals'].append(dict(entity_id=entity, instance=instance, timestamp=t))
                s['removals'] = s['removals'][-64:]
                cast = s['cast']
                if cast and entity == cast['boss_id'] and instance == cast['shield_instance']:
                    cast['shield_removed_at'] = t
                    self._event(s, 'boss_self_buff_removed', t, entity_id=entity,
                                instance=instance, skill_id=BOSS_SHIELD)
            return
        if typ == 20272 and len(b) == 8:
            entity = struct.unpack('<Q', b)[0]
            if entity in s['markers']: s['markers'][entity]['dead'] = True

    def _target(self, s):
        cast = s['cast']
        if not cast or cast['element'] != '불': return {}
        matches = [n for n in s['shamans'].values()
                   if n['element'] == '불' and n['role_confirmed']]
        return matches[0] if len(matches) == 1 else {}

    def _departure(self, s):
        cast = s['cast']; npc = self._target(s)
        if not cast or not npc or cast['failure'] or cast['success'] or npc['dead']: return
        markers = [m for m in s['markers'].values() if m['created_at'] >= cast['started_at']
                   and m['owner_id'] == cast['boss_id'] and m['target_id'] == cast['target_id']
                   and m['position'] and m['frozen']]
        candidates = []
        peers = [n for n in s['shamans'].values() if n['role_confirmed'] and n['entity_id'] != npc['entity_id']]
        if not peers: return
        # A geometric slot check, not a calibrated movement/arrival tolerance.
        radius = min(math.hypot(n['x'] - npc['x'], n['y'] - npc['y']) for n in peers) / 2
        for marker in markers:
            pos, frozen = marker['position'], marker['frozen']
            if abs(pos['timestamp'] - frozen['timestamp']) > PAIR_WINDOW: continue
            if max(abs(pos[key] - frozen[key]) for key in ('x', 'y', 'heading')) > .001: continue
            distance = math.hypot(pos['x'] - npc['x'], pos['y'] - npc['y'])
            if radius <= 0 or distance >= radius: continue
            candidates.append((marker, distance))
        if len(candidates) != 1: return
        marker, distance = candidates[0]
        if not cast['departure_candidate']:
            cast['departure_candidate'] = True; cast['marker_id'] = marker['entity_id']
            cast['locked_at'] = marker['frozen']['timestamp']; cast['locked_position'] = dict(marker['position'])
            cast['target_distance'] = distance
            if cast['released_at'] is None: cast['stage'] = 'lock_candidate'
            self._event(s, 'departure_candidate', cast['locked_at'], marker_id=marker['entity_id'],
                target_shaman_id=npc['entity_id'], distance=distance,
                evidence='bound_marker_position_and_cleared_reference')

    def _success(self, s):
        cast = s['cast']; npc = self._target(s)
        if not cast or not npc or cast['failure'] or cast['released_at'] is None: return
        for hit in s['breaks']:
            if not (hit['entity_id'] == npc['entity_id'] and hit['kind'] == 3 and
                    hit['flag'] == 1 and hit['caster'] == cast['boss_id']): continue
            if not 0 <= hit['timestamp'] - cast['released_at'] <= RELEASE_IMPACT_WINDOW: continue
            boss_status = s['statuses'].get((cast['boss_id'], 'Groggy'))
            status_ok = boss_status is not None and abs(boss_status - hit['timestamp']) <= PAIR_WINDOW
            removal = next((r for r in s['removals'] if r['entity_id'] == cast['boss_id'] and
                r['instance'] == cast['shield_instance'] and abs(r['timestamp'] - hit['timestamp']) <= PAIR_WINDOW), None)
            if not status_ok and removal is None: continue
            already_observed = cast['success']
            cast['success'] = True; cast['stage'] = 'success_observed'; cast['impact_at'] = hit['timestamp']
            cast['success_evidence'] = dict(correct_shaman_break=hit,
                boss_groggy_correlated=status_ok, matched_boss_self_buff_removed=removal is not None)
            if not already_observed:
                self._event(s, 'success_observed', hit['timestamp'], boss_id=cast['boss_id'],
                    shaman_id=npc['entity_id'], evidence=cast['success_evidence'])
            return

    def snapshot(self, now=None):
        with self.lock:
            if now is None:
                now = (self.last_time or 0.) + (max(0., time.monotonic() - self.fed_at) if self.fed_at is not None else 0.)
            s = max(self.scopes.values(), key=lambda x: x['last_seen']) if self.scopes else None
            cast = s['cast'] if s else None
            fresh = bool(s and now - s['last_seen'] <= STREAM_FRESHNESS)
            stage = cast['stage'] if cast else 'waiting'
            if s and not fresh: stage = 'stale'
            active = bool(cast and fresh and not cast['success'] and not cast['failure'] and not cast['boss_dead'])
            target = self._target(s) if s else {}
            result = dict(stage=stage, active_fresh=fresh, phase_active=active,
                stream_id=s['stream_id'] if s else None, generation=s['generation'] if s else None,
                boss=s['bosses'].get(cast['boss_id']) if cast else None,
                target_shaman=target or None, cast_target_id=cast['target_id'] if cast else None,
                element=cast['element'] if active else None,
                last_observed_element=cast['element'] if cast else None,
                cast=cast, shamans=list(s['shamans'].values()) if s else [],
                departure_candidate=bool(cast and cast['departure_candidate'] and fresh and not cast['failure']),
                success_observed=bool(cast and cast['success']), events=list(self.events),
                malformed_messages=self.malformed, self_identity_confirmed=False,
                departure_control_verified=False, game_input_allowed=False,
                profile='observed-fire-20261115', blocker='movement_basis_and_departure_control_unverified')
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


def run_selftest():
    """Exercise the observed profile with synthetic data only, without input."""
    state = SpearPacketState()
    boss, target, marker, fire = 101, 202, 303, 404
    def feed(typ, body, at): state.feed(typ, bytes(body), at, 'synthetic-server')
    def action(actor, name, destination, skill):
        text = name.encode('utf-16le'); body = bytearray(41 + len(text))
        struct.pack_into('<QI', body, 0, actor, len(text)); body[12:12 + len(text)] = text
        struct.pack_into('<QI', body, 29 + len(text), destination, skill)
        return body
    body = bytearray(1636); struct.pack_into('<Q', body, 0, boss)
    body[638:662] = SPEAR_SIGNATURE; body[1100:1108] = POSITION_MARKER
    struct.pack_into('<fff', body, 1108, 0, 163, math.pi)
    feed(20051, body, 0)
    slots = ((10, 160), (10, 151), (10, 142), (-10, 142), (-10, 160), (-10, 151))
    for index, (skill, (_, suffix)) in enumerate(SHIELDS.items()):
        entity = fire + index; x, y = slots[index]
        body = bytearray(1554); struct.pack_into('<Q', body, 0, entity)
        struct.pack_into('<I', body, 622, skill); body[1064:1072] = POSITION_MARKER
        struct.pack_into('<fff', body, 1072, x, y, math.pi)
        feed(20051, body, .1)
        feed(170064, action(entity, 'Abyss_Shield_' + suffix, target, skill), .2)
    feed(170069, struct.pack('<QQIfIQ', boss, 55, BOSS_SHIELD, -1e10, 1, boss), .5)
    feed(170064, action(boss, 'Penetrate_Casting', target, FIRE_SKILL), 1)
    body = bytearray(211)
    for offset, value in ((0, marker), (67, boss), (182, target)):
        struct.pack_into('<Q', body, offset, value)
    feed(20066, body, 1.1)
    for at in range(2, 8): feed(20132, b'', at)
    feed(171128, struct.pack('<Qfff', marker, 9, 160, math.pi), 7)
    body = bytearray(41)
    struct.pack_into('<QfffQ', body, 0, marker, 9, 160, math.pi, 2**64 - 1)
    feed(171126, body, 7)
    feed(170064, action(boss, 'Penetrate', target, FIRE_SKILL), 8.5)
    feed(170068, struct.pack('<QIfQI', fire, 3, 3., boss, 1), 8.8)
    text = 'Groggy'.encode('utf-16le'); body = bytearray(37 + len(text))
    struct.pack_into('<QI', body, 0, boss, len(text)); body[12:12 + len(text)] = text
    feed(170063, body, 8.8)
    feed(170070, struct.pack('<QQ', boss, 55), 8.8)
    result = state.snapshot(now=8.8)
    assert result['success_observed'] and result['departure_candidate']
    assert result['stage'] == 'success_observed' and result['last_observed_element'] == '불'
    assert len([s for s in result['shamans'] if s['role_confirmed']]) == 6
    assert result['cast']['success_evidence']['matched_boss_self_buff_removed']
    assert not result['game_input_allowed'] and not result['departure_control_verified']
    result['synthetic_only'] = True
    return result
