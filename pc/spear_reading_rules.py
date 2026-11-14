"""Read-only decision rules for offline EntityMap observations.

No fuzzy correction of numbers/elements, pixel-to-world conversion or game input.
"""
from collections import Counter
import re

ELEMENTS=frozenset(('불','독','얼음','번개','정신','어둠'))

def parse_coordinates(text):
 text=re.sub(r'\s','',text).replace('−','-')
 match=re.fullmatch(r'[Xx]:?([+-]?\d+(?:\.\d{1,2})?),?[Yy]:?([+-]?\d+(?:\.\d{1,2})?)',text)
 if not match:return None
 return tuple(float(v) for v in match.groups())

def coordinate_consensus(readings):
 parsed=[parse_coordinates(text) for text in readings]
 if len(parsed)<2 or any(value is None for value in parsed):return None
 return parsed[0] if all(value==parsed[0] for value in parsed) else None

def parse_element(text,prefix):
 compact=re.sub(r'[\s·ㆍ:：|.,\-]','',text)
 if not compact.startswith(prefix):return None
 value=compact[len(prefix):]
 return value if value in ELEMENTS else None

def element_consensus(readings,prefix):
 values=[parse_element(s,prefix) for s in readings]
 counts=Counter(v for v in values if v is not None)
 # Empty/error variants do not vote; conflicting valid elements veto.
 return next(iter(counts)) if len(counts)==1 and max(counts.values())>=2 else None

def pattern_pair(boss_readings,target_readings):
 boss=element_consensus(boss_readings,'투창병')
 target=element_consensus(target_readings,'정답')
 return boss if boss is not None and boss==target else None

def decision(*,pattern,coordinates,self_identity_confirmed=False,target_position_confirmed=False,success_confirmed=False):
 # These experiments do not provide verified identity/positions/success.
 return {'pattern':pattern,'coordinates':coordinates,'self_identity_confirmed':bool(self_identity_confirmed),'target_position_confirmed':bool(target_position_confirmed),'success_confirmed':bool(success_confirmed),'movement_allowed':False}

