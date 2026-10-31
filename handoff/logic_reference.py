"""Platform-independent decision functions extracted from PC v0.12. No capture/input code."""

DUNGEONS={'허상의 정박지':'dungeon_dock','광기의 동굴':'dungeon_cave','흩어진 물길':'dungeon_water'}

def dungeon_list_visible(valid):
    return sum(k in valid for k in DUNGEONS.values())>=2

def screen_stage(valid, previous='search'):
    """Recover from observable screens, independently of earlier input history."""
    if 'partycancel' in valid:return 'queue'
    if 'challenge_title' in valid or 'partyready' in valid:return 'queue'
    if dungeon_list_visible(valid):return 'dungeon_list'
    if 'retry' in valid or 'other_dungeon' in valid or 'touch' in valid:return 'result'
    if 'skip' in valid or 'break' in valid:return 'battle'
    if 'enter' in valid:return 'ready'
    if 'compass' in valid:return 'queue'
    if 'hud' in valid:return 'battle'
    return previous

def screen_action(valid,party_mode='1인',target='허상의 정박지',party_role='파티장'):
    if 'partycancel' in valid:return None
    if 'challenge_title' in valid:return 'challenge' if 'challenge' in valid else None
    if 'partyready' in valid:return 'partyready'
    if dungeon_list_visible(valid):
        key=DUNGEONS.get(target)
        return key if party_mode=='2~3인' and key in valid else None
    if 'retry' in valid or 'other_dungeon' in valid:
        if party_mode=='4인' and party_role=='파티원':return None
        key='other_dungeon' if party_mode=='2~3인' else 'retry'
        return key if key in valid else None
    action=next((k for k in ('touch','skip','enter','compass') if k in valid),None)
    if party_mode=='4인' and party_role=='파티원' and action=='enter':return None
    return action
