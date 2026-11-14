"""Read-only EntityMap observations. Screen pixels are never world coordinates.

There is deliberately no movement controller, packet reader or success inference.
"""
from dataclasses import dataclass, field
import re
import cv2
import numpy as np


@dataclass(frozen=True)
class Label:
    text: str
    score: float
    box: tuple[int, int, int, int]

    @property
    def center(self):
        x, y, w, h = self.box
        return x + w / 2, y + h / 2


@dataclass
class Observation:
    pattern: str = 'unknown'
    target: Label | None = None
    element: str | None = None
    self_pixel: tuple[float, float] | None = None
    coordinates: tuple[float, float] | None = None
    stars: list = field(default_factory=list)
    grid: list = field(default_factory=list)
    cells: dict = field(default_factory=dict)
    confidence: float = 0.
    reason: str = '인식 대기'
    # Always false until target ownership, movement and completion are validated.
    movement_allowed: bool = False
    success_confirmed: bool = False


ELEMENTS = ('정신', '어둠', '번개', '얼음', '독', '불')


def element_of(text, prefix):
    # Accept punctuation/spacing variation, never fuzzy-match a different element.
    compact = re.sub(r'[\s·:：|.,]', '', text)
    return next((e for e in ELEMENTS if compact == prefix + e), None)


def read_labels(rgb, ocr):
    """Keep positions and OCR confidence; use colour-preserving enlarged input."""
    enlarged = cv2.resize(rgb, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    data = ocr.api.image_to_data(enlarged, lang='kor',
        config=ocr.config + ' --psm 11', output_type=ocr.api.Output.DICT, timeout=2)
    lines = {}
    for i, text in enumerate(data['text']):
        if not text.strip() or float(data['conf'][i]) < 0:
            continue
        key = tuple(data[k][i] for k in ('block_num', 'par_num', 'line_num'))
        lines.setdefault(key, []).append(i)
    labels = []
    for indices in lines.values():
        left = min(data['left'][i] for i in indices)
        top = min(data['top'][i] for i in indices)
        right = max(data['left'][i] + data['width'][i] for i in indices)
        bottom = max(data['top'][i] + data['height'][i] for i in indices)
        labels.append(Label(' '.join(data['text'][i] for i in indices),
            min(float(data['conf'][i]) for i in indices) / 100,
            (left//3, top//3, max(1, (right-left)//3), max(1, (bottom-top)//3))))
    return labels


def triangles(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    yellow = cv2.inRange(hsv, (18, 100, 140), (38, 255, 255))
    contours, _ = cv2.findContours(yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (4 <= w <= 35 and 4 <= h <= 35 and cv2.contourArea(c) >= 6):
            continue
        polygon = cv2.approxPolyDP(c, .07*cv2.arcLength(c, True), True)
        if len(polygon) == 3:
            found.append((x+w/2, y+h/2))
    return found


def green_stars(rgb):
    """Require a green outline AND a concave bright star, not the range ring."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    bright = cv2.inRange(hsv, (0, 0, 190), (179, 95, 255))
    green = cv2.inRange(hsv, (38, 65, 100), (90, 255, 255))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    stars = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (7 <= w <= 40 and 7 <= h <= 40 and .65 < w/h < 1.5):
            continue
        area = cv2.contourArea(c)
        hull = cv2.contourArea(cv2.convexHull(c))
        polygon = cv2.approxPolyDP(c, .025*cv2.arcLength(c, True), True)
        patch = green[max(0,y-10):y+h+10, max(0,x-10):x+w+10]
        if hull and .32 < area/hull < .85 and 8 <= len(polygon) <= 12 and cv2.countNonZero(patch) >= 15:
            stars.append((x+w/2, y+h/2))
    return stars


def find_grid(rgb):
    """Require all nine current-frame rings; an occluded board stays unknown."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    rings = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1, 12,
        param1=90, param2=20, minRadius=9, maxRadius=28)
    if rings is None:
        return []
    rings = rings[0][:80]
    fits = []
    for x, y, r in rings:
        for bx, by, br in rings:
            spacing = bx-x
            if not (1.9*r < spacing < 3.5*r and abs(by-y) < r*.3 and abs(br-r) < r*.3):
                continue
            expected = [(float(x+col*spacing), float(y+row*spacing)) for row in range(3) for col in range(3)]
            used = []
            for px, py in expected:
                distances = np.hypot(rings[:,0]-px, rings[:,1]-py)
                k = int(np.argmin(distances))
                if distances[k] > spacing*.18 or abs(rings[k,2]-r) > r*.35:
                    break
                used.append(k)
            if len(set(used)) == 9:
                fits.append(expected)
    if not fits:
        return []
    first = fits[0]
    if any(np.hypot(f[0][0]-first[0][0], f[0][1]-first[0][1]) > 10 for f in fits[1:]):
        return []
    return first


def red_x(rgb, center, radius):
    """Read red-ring/bright-X geometry only; never infer a completed mechanic."""
    x,y = map(round, center)
    r = max(5, round(radius))
    patch = rgb[max(0,y-r):y+r+1, max(0,x-r):x+r+1]
    if patch.shape[:2] != (r*2+1,r*2+1):
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    red = ((hsv[:,:,0] < 12) | (hsv[:,:,0] > 165)) & (hsv[:,:,1] > 80) & (hsv[:,:,2] > 100)
    yy,xx = np.mgrid[-r:r+1,-r:r+1]
    ring = (np.hypot(xx,yy) > r*.7) & (np.hypot(xx,yy) < r*1.1)
    if np.count_nonzero(red & ring) < 10:
        return False
    white = (hsv[:,:,1] < 110) & (hsv[:,:,2] > 180)
    core = (abs(xx) < r*.6) & (abs(yy) < r*.6)
    diagonal = (abs(xx-yy) <= 2) | (abs(xx+yy) <= 2)
    ink = white & core
    return (np.count_nonzero(ink) >= 12 and
            np.count_nonzero(ink & diagonal) / np.count_nonzero(ink) > .8 and
            np.count_nonzero(ink & (xx > 1) & (yy > 1)) >= 2 and
            np.count_nonzero(ink & (xx > 1) & (yy < -1)) >= 2)


def observe(rgb, labels=()):
    result = Observation()
    reliable = [t for t in labels if t.score >= .65]
    answers = [(t, element_of(t.text, '정답')) for t in reliable]
    answers = [(t, e) for t, e in answers if e]
    spears = {element_of(t.text, '투창병') for t in reliable} - {None}
    if len(answers) == 1 and spears == {answers[0][1]}:
        result.pattern = 'spear'
        result.target, result.element = answers[0]
        result.confidence = min(result.target.score,
            max(t.score for t in reliable if element_of(t.text, '투창병') == result.element))
        result.reason = '정답 이름표 후보 · 표식 위치/내 대상 여부 미검증'
    elif answers or spears:
        result.reason = '정답/투창병 속성이 불명확하거나 서로 다름'
    self_labels = [t for t in reliable if re.match(r'^나\s*[:：]', t.text)]
    candidates = []
    for px, py in triangles(rgb):
        if any(abs(px-t.box[0]) <= 40 and abs(py-t.center[1]) <= 18 for t in self_labels):
            candidates.append((px, py))
    if len(candidates) == 1 and len(self_labels) == 1:
        result.self_pixel = candidates[0]
    coords = []
    for t in reliable:
        m = re.fullmatch(r'\s*X\s*[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*[,;]?\s*Y\s*[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*', t.text, re.I)
        if m:
            coords.append(tuple(map(float, m.groups())))
    if len(coords) == 1:
        result.coordinates = coords[0]
    result.stars = green_stars(rgb)
    result.grid = find_grid(rgb)
    if result.grid and not answers and not spears:
        result.pattern = 'bingo'
        result.reason = '빙고 격자 후보 · 초록 별은 추천 후보, O/X 확정과 성공 판정 보류'
        result.confidence = .65  # heuristic evidence score, not probability
        radius = np.hypot(result.grid[1][0]-result.grid[0][0],
                          result.grid[1][1]-result.grid[0][1]) / 2.6
        for index, point in enumerate(result.grid):
            if any(np.hypot(point[0]-s[0], point[1]-s[1]) < 12 for s in result.stars):
                result.cells[index] = 'recommendation_candidate'
            elif red_x(rgb, point, radius):
                result.cells[index] = 'boss_x_candidate'
    return result


class StableObserver:
    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.count = 0

    def update(self, observation):
        # Relative screen position is part of identity (EntityMapFollowSelf shifts).
        target = observation.target.center if observation.target else None
        key = (observation.pattern, observation.element,
               tuple(round(v/4) for v in target) if target else None,
               tuple(sorted(observation.cells.items())))
        if observation.pattern == 'unknown':
            self.reset()
            return False
        self.count = self.count+1 if key == self.key else 1
        self.key = key
        return self.count >= 3
