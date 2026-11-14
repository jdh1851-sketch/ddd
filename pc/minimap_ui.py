"""Observation-only tab; no game-input API is called from this module."""
import ctypes as C
from ctypes import wintypes as W
import time
import tkinter as tk
from tkinter import ttk, filedialog
import cv2
import numpy as np
from PIL import Image, ImageGrab, ImageTk
from fishing import KoreanOCR
from scan_async import LatestScan
from minimap_vision import read_labels, observe, StableObserver


class MinimapPanel:
    def __init__(self, parent, host):
        self.host = host
        self.worker = LatestScan()
        self.ocr = None
        self.stable = StableObserver()
        self.source = None
        self.next_scan = 0
        self.size = None
        self.last_frame = 0
        self.image_generation = 0
        self.pending_image = None
        self.report = tk.StringVar(value='관찰 전용 · 이동/공격 입력 없음')
        self.detail = tk.StringVar(value='모비레이의 좌표 미니맵 창을 열거나 캡처 이미지를 선택하세요.')
        ttk.Label(parent, text='흩어진 물길 · 미니맵 관찰', font=('맑은 고딕', 18, 'bold')).pack(pady=12)
        ttk.Label(parent, text='초록 별 = 추천 후보 / 성공 여부 미검증 / 화면 픽셀 ≠ 게임 좌표').pack()
        buttons = ttk.Frame(parent); buttons.pack(pady=8)
        ttk.Button(buttons, text='캡처 이미지 열기', command=self.load).pack(side='left', padx=5)
        ttk.Button(buttons, text='실시간 창 관찰 F8', command=self.host.toggle).pack(side='left', padx=5)
        ttk.Label(parent, textvariable=self.report, wraplength=850).pack(pady=8)
        ttk.Label(parent, textvariable=self.detail, wraplength=850, justify='left').pack()
        self.preview = ttk.Label(parent, text='정답 이름표·추천 별 후보에만 표시합니다.')
        self.preview.pack(pady=10)

    def invalidate(self):
        self.image_generation += 1
        self.pending_image = None
        self.worker.reset()
        self.stable.reset()
        self.preview.configure(image='', text='이전 관찰 표시 제거됨')
        self.report.set('관찰 중지 · 현재 목표 없음 · 입력 없음')
        self.detail.set('기믹 성공 판정: 미검증')

    def stopped(self):
        self.invalidate()

    def close(self):
        self.worker.close()

    def find_window(self):
        u = self.host.user32
        found = []
        @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def callback(handle, _):
            title = C.create_unicode_buffer(u.GetWindowTextLengthW(handle)+1)
            u.GetWindowTextW(handle, title, len(title))
            if title.value.strip() == '좌표 미니맵' and u.IsWindowVisible(handle):
                found.append(handle)
            return True
        u.EnumWindows(callback, 0)
        if len(found) != 1:
            raise ValueError('제목이 ‘좌표 미니맵’인 창 하나를 열어주세요. 다른 화면을 자동 선택하지 않습니다.')
        return found[0]

    def start(self):
        self.invalidate()
        self.source = self.find_window()
        self.size = None
        self.last_frame = time.monotonic()
        self.next_scan = 0

    def analyze(self, rgb):
        if self.ocr is None:
            self.ocr = KoreanOCR()
        labels = read_labels(rgb, self.ocr)
        return rgb, observe(rgb, labels), labels

    def load(self):
        self.host.stop('이미지 관찰로 전환')
        path = filedialog.askopenfilename(filetypes=[('캡처', '*.png *.jpg *.jpeg *.bmp')])
        if not path:
            return
        try:
            with Image.open(path) as image:
                rgb = np.array(image.convert('RGB'))
            # Image loading also uses the worker; old results cannot replace a new source.
            self.worker.reset()
            self.source = None
            self.pending_image = rgb
            self.poll_image(self.image_generation)
        except Exception as exc:
            self.report.set('이미지 오류: '+str(exc))

    def poll_image(self, generation):
        if generation != self.image_generation:
            return
        try:
            result = self.worker.poll(max_age=5)
            if result is not None:
                self.render(*result, live=False)
                return
            if getattr(self, 'pending_image', None) is not None:
                if self.worker.submit(self.analyze, self.pending_image):
                    self.pending_image = None
            elif self.worker.future is None:
                self.report.set('분석 시간 초과 · 목표 없음')
                return
            self.host.root.after(80, lambda: self.poll_image(generation))
        except Exception as exc:
            self.invalidate()
            self.report.set('OCR/이미지 분석 오류: '+str(exc))

    def step(self, now):
        u = self.host.user32
        if not self.source or not u.IsWindow(self.source) or u.IsIconic(self.source):
            raise ValueError('미니맵 창이 닫혔거나 최소화되어 관찰을 중단합니다.')
        x, y, w, h = self.host.window_rect(self.source)
        if w < 100 or h < 100:
            raise ValueError('미니맵 창 크기를 확인하세요.')
        if self.size != (x,y,w,h):
            self.invalidate()
            self.size = (x,y,w,h)
        # A foreground game is fine, but another window covering the map is not.
        for fx, fy in ((.15,.15),(.5,.5),(.85,.85),(.15,.85),(.85,.15)):
            point = W.POINT(x+int(w*fx), y+int(h*fy))
            if u.GetAncestor(u.WindowFromPoint(point), 2) != self.source:
                self.host.stop('미니맵이 다른 창에 가려져 관찰 중단')
                return
        result = self.worker.poll(max_age=5)
        if result is not None:
            self.last_frame = now
            self.render(*result, live=True)
        if now-self.last_frame > 6:
            raise ValueError('새 인식 결과가 없어 관찰 중단')
        if now >= self.next_scan and self.worker.future is None:
            rgb = np.array(ImageGrab.grab(bbox=(x,y,x+w,y+h), all_screens=True))
            self.worker.submit(self.analyze, rgb)
            self.next_scan = now+.5

    def render(self, rgb, observation, labels, live):
        stable = self.stable.update(observation) if live else False
        frame = rgb.copy()
        if observation.target:
            x,y,w,h = observation.target.box
            cv2.rectangle(frame, (x,y), (x+w,y+h), (255,160,40), 2)
        for x,y in observation.stars:
            cv2.circle(frame, (round(x),round(y)), 17, (255,160,40), 2)
        if observation.self_pixel:
            x,y = map(round, observation.self_pixel)
            cv2.circle(frame, (x,y), 12, (255,220,30), 2)
        for i,(x,y) in enumerate(observation.grid):
            cv2.putText(frame, str(i+1), (round(x)-5,round(y)+5), cv2.FONT_HERSHEY_SIMPLEX, .4, (255,210,100), 1)
        pattern = {'unknown':'불명확', 'spear':'투창 후보', 'bingo':'빙고 후보'}[observation.pattern]
        confidence = f'{observation.confidence:.0%}' if observation.pattern != 'unknown' else '—'
        self.report.set(f'패턴: {pattern} | 근거 점수: {confidence} (성공 확률 아님) | '+
                        ('3회 연속 관찰' if stable else '단일/불안정 관찰')+' | 입력 없음')
        target = f'정답·{observation.element} 이름표' if observation.element else f'추천 별 후보 {len(observation.stars)}개'
        cells = ', '.join(f'{i//3+1}행 {i%3+1}열: '+
            {'recommendation_candidate':'추천 후보','boss_x_candidate':'보스 X 후보'}[kind]
            for i,kind in sorted(observation.cells.items())) or '미확인'
        self.detail.set(f'목표: {target}\n내 위치(화면 픽셀): {observation.self_pixel or "미확인"} · '+
                        f'하단 X/Y: {observation.coordinates or "미확인"}\n격자: {cells}\n{observation.reason}\n'+
                        '내 대상 여부·O/X 확정·기믹 성공: 미검증 · 이동 단계 잠김')
        image = Image.fromarray(frame)
        image.thumbnail((850,480))
        self.photo = ImageTk.PhotoImage(image)
        self.preview.configure(image=self.photo, text='')
