"""Fishing vision and state machine. No Windows or Tk dependencies."""
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import sys
import cv2
import numpy as np

PHRASES = {
    'heavy': '묵직한 느낌', 'lucky': '예감이 좋다',
    'float': '찌가 흔들렸다', 'bite': '입질이 왔다',
    'response': '반응이 왔다', 'caught': '뭔가 걸렸다',
}
KEEP = frozenset(('heavy', 'lucky', 'float', 'bite'))

def normalize(text):
    return re.sub(r'[^가-힣a-zA-Z0-9]', '', text)

def classify(text):
    text = normalize(text)
    # Explicit OCR confusions observed in the supplied compressed videos.
    if any(alias in text for alias in ('씨가은들렸다','피가흔들렸다','찌가은들렸다')):
        return 'float'
    for key, phrase in PHRASES.items():
        if normalize(phrase) in text:
            return key
    return None

def result_kind(text):
    text = normalize(text)
    if '낚았다' in text:
        return 'success'
    if any(word in text for word in ('놓쳤', '놓쳐', '실패', '도망')):
        return 'failed'
    return None

def crop(rgb, roi):
    h, w = rgb.shape[:2]
    x, y, rw, rh = roi
    return rgb[max(0, int(y*h)):min(h, int((y+rh)*h)),
               max(0, int(x*w)):min(w, int((x+rw)*w))]

def gauge_level(rgb):
    """ROI must tightly enclose the full (green + empty) gauge, excluding icon."""
    if rgb.size == 0:
        return None
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, (35, 100, 110), (90, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w >= 4 and w > h*1.7 and cv2.contourArea(contour) >= 8:
            candidates.append((w, x, y, h))
    if not candidates:
        return None
    width, x, y, height = max(candidates)
    return min(1., width / rgb.shape[1])

def idle_score(rgb, template):
    if rgb.size == 0 or template is None:
        return 0.
    patch = cv2.cvtColor(cv2.resize(rgb, (96, 96)), cv2.COLOR_RGB2GRAY)
    target = cv2.cvtColor(cv2.resize(template, (96, 96)), cv2.COLOR_RGB2GRAY)
    if target.std() < 8:
        return 0.
    return float(cv2.matchTemplate(patch, target, cv2.TM_CCOEFF_NORMED)[0, 0])

class KoreanOCR:
    def __init__(self, executable=''):
        import pytesseract
        self.api = pytesseract
        if executable:
            if not Path(executable).is_file():
                raise ValueError('Tesseract 실행 파일 경로를 확인하세요.')
            self.api.pytesseract.tesseract_cmd = executable
        elif (Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'ocr'/'tesseract.exe').is_file():
            self.api.pytesseract.tesseract_cmd = str(Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'ocr'/'tesseract.exe')
        elif shutil.which('tesseract'):
            self.api.pytesseract.tesseract_cmd = shutil.which('tesseract')
        else:
            default = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
            if default.is_file():
                self.api.pytesseract.tesseract_cmd = str(default)
            else:
                raise ValueError('한국어 Tesseract OCR을 설치하거나 실행 파일 경로를 지정하세요.')
        data_dir = Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'tessdata'
        self.config = f'--tessdata-dir "{data_dir}"' if (data_dir/'kor.traineddata').exists() else ''
        if 'kor' not in self.api.get_languages(config=self.config):
            raise ValueError('Tesseract 한국어 데이터(kor.traineddata)가 필요합니다.')

    def read(self, rgb):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        # Game messages are white. Removing the background improves Korean OCR.
        mask = cv2.inRange(rgb, (180, 180, 180), (255, 255, 255))
        image = cv2.resize(255-mask, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = self.api.image_to_string(image, lang='kor', config=self.config+' --psm 6', timeout=2).strip()
        if classify(text) or result_kind(text):
            return text
        fallback = cv2.resize(255-cv2.threshold(gray,190,255,cv2.THRESH_BINARY)[1],None,fx=3,fy=3)
        return self.api.image_to_string(fallback,lang='kor',config=self.config+' --psm 11',timeout=2).strip()

@dataclass
class Signal:
    gauge: float | None = None
    phrase: str | None = None
    idle: bool = False
    result: str | None = None

class FishingController:
    """Returns an intent once; caller must acknowledge after sending input."""
    def __init__(self, keep=KEEP):
        self.keep = set(keep)
        self.reset()

    def reset(self):
        self.state = 'waiting'
        self.phrase = None
        self.high = self.low = None
        self.falling = False
        self.rises = self.idle_frames = self.absent = 0
        self.pending = None
        self.since = 0.
        self.last_action = -100.
        self.catches = self.cancels = self.failures = 0

    def interrupted(self):
        # Foreground loss invalidates derivatives, but preserves a sent action.
        self.high = self.low = None
        self.falling = False
        self.rises = self.idle_frames = self.absent = 0
        if self.state == 'tracking':
            self.state = 'waiting'
            self.phrase = None

    def sent(self, action, now):
        self.last_action = now
        self.pending = action
        self.since = now
        self.state = 'casting' if action == 'cast' else 'settling'
        self.idle_frames = self.absent = self.rises = 0

    def update(self, s, now):
        if self.pending:
            if self.pending == 'cast':
                if s.gauge is not None:
                    self.pending = None
                    self.state = 'waiting'
                elif now-self.since > 20:
                    self.state = '입질 확인 시간 초과'
                    return 'stop'
                else:
                    return None
            else:
                if s.gauge is None:
                    self.absent += 1
                else:
                    self.absent = 0
                confirmed = (self.pending == 'cancel' and self.absent >= 3 and s.idle)
                confirmed |= (self.pending == 'reel' and bool(s.result))
                if confirmed:
                    if self.pending == 'cancel': self.cancels += 1
                    elif s.result == 'success': self.catches += 1
                    else: self.failures += 1
                    self.pending = None
                    self.state = 'waiting'
                    self.phrase = None
                    self.high = self.low = None
                    self.falling = False
                    self.rises = 0
                elif now-self.since > 12:
                    self.state = '입력 후 결과 확인 실패'
                    return 'stop'
                return None
        if s.gauge is not None:
            self.absent = 0
            self.idle_frames = 0
            if s.phrase and self.phrase is None:
                self.phrase = s.phrase
                self.state = 'tracking'
                self.high = self.low = s.gauge
            if self.phrase is None:
                return None
            if self.phrase not in self.keep:
                return 'cancel'
            level = s.gauge
            self.high = max(self.high if self.high is not None else level, level)
            if not self.falling:
                self.low = level
                self.falling = self.high-level >= .15
            else:
                self.low = min(self.low, level)
            self.rises = self.rises+1 if self.falling and level-self.low >= .025 else 0
            if self.rises >= 2:
                return 'reel'
        else:
            if self.phrase:
                self.absent += 1
                if self.absent < 8:
                    return None
                # A missed rebound must not turn into a blind reel or a new cast.
                self.state = '게이지 소실 · 화면 확인 필요'
                return 'stop'
            self.idle_frames = self.idle_frames+1 if s.idle else 0
            if self.idle_frames >= 3 and now-self.last_action >= 3:
                return 'cast'
        return None
