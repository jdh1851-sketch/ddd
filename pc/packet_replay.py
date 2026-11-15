"""Read-only replay with independently timestamped coordinate-window evidence."""
from packet_decode import analyze_archive
from packet_identity import IdentityTracker, correlate_evidence


def attach_identity(result, identity):
    identity=dict(identity)
    scope=identity.get('self_scope') or {}
    own=identity.get('self_entity_id')
    valid=(isinstance(own,int) and not isinstance(own,bool) and 0<own<2**64 and
        isinstance(scope.get('source_id'),str) and bool(scope['source_id']) and
        isinstance(scope.get('stream_id'),str) and bool(scope['stream_id']) and
        isinstance(scope.get('generation'),int) and not isinstance(scope['generation'],bool) and scope['generation']>=0)
    if identity.get('confirmed') and not valid:
        identity.update(confirmed=False,self_entity_id=None,reason='invalid_identity_scope')
    result['identity']=identity
    result['self_identity_confirmed']=bool(identity.get('confirmed'))
    spear=result.get('spear',{})
    own=identity.get('self_entity_id')
    same_scope=bool(valid and identity.get('confirmed') and scope.get('stream_id')==spear.get('stream_id')
                    and scope.get('generation')==spear.get('generation'))
    spear['self_entity_id']=own if same_scope else None
    spear['self_identity_confirmed']=same_scope
    spear['target_is_self']=(spear.get('cast_target_id')==own) if same_scope else None
    result['game_input_allowed']=False
    return result


def inspect_archive(path):
    tracker=IdentityTracker()
    try:
        result=analyze_archive(path,position_observer=tracker.add_position,reset_observer=tracker.reset_stream)
        identity=correlate_evidence(path,tracker.position_samples())
        return attach_identity(result,identity)
    finally:tracker.close()
