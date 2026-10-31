"""Fishing tab and calibration; imported only by the Windows application."""
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import cv2
import numpy as np
from PIL import Image, ImageTk, ImageGrab
from fishing import (PHRASES, KEEP, KoreanOCR, FishingController, Signal,
                     classify, result_kind, crop, gauge_level, idle_score)

BG = '#f2f5fa'
STATE_NAMES = {'waiting': '입질 대기', 'tracking': '게이지 반등 관찰 중',
               'casting': '낚시 시작 · 입질 대기', 'settling': '입력 완료 · 결과 확인 중'}

class FishingPanel:
    def __init__(self, parent, app, home):
        self.app, self.home = app, Path(home)
        self.config_path = self.home/'fishing_settings.json'
        self.engine = FishingController()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fishing-ocr')
        self.future = None
        self.epoch = 0
        self.last_ocr = -100.
        self.ocr_text = ''
        self.ocr_phrase = self.ocr_result = None
        self.ocr_time = -100.
        self.last_phrase = None
        self.phrase_stable = 0
        self.preview_time = -100.
        self.rois = {}
        self.template = None
        self.size = None
        self.active = False
        self.mode = tk.StringVar(value='observe')
        self.limit = tk.StringVar(value='0')
        self.timeout = tk.StringVar(value='15')
        self.cancel_key = tk.StringVar(value='Esc')
        self.tesseract = tk.StringVar(value='')
        self.message = tk.StringVar(value='문구: 대기 중')
        self.state = tk.StringVar(value='상태: 중지됨')
        self.counter = tk.StringVar(value='성공 0회   취소 0회   실패 0회')
        self.calibration = tk.StringVar(value='화면 영역 3개를 먼저 지정하세요.')
        self.keep = {key: tk.BooleanVar(value=True) for key in KEEP}
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        self.canvas=canvas
        scroll = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y'); canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        self.body = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        tk.Label(self.body, text='낚시', bg=BG, fg='#24334a', font=('맑은 고딕', 24, 'bold')).pack(anchor='w', padx=24, pady=(16, 2))
        tk.Label(self.body, text='중급 이상 낚기 · 하급 취소 후 다시 시작', bg=BG, fg='#64748b').pack(anchor='w', padx=24, pady=(0, 12))
        banner = tk.Frame(self.body, bg='#172b4d', padx=16, pady=12); banner.pack(fill='x', padx=22, pady=(0,12))
        tk.Label(banner, text='F8 시작 / 일시정지     F9 즉시정지', bg='#172b4d', fg='white', font=('맑은 고딕',16,'bold')).pack(anchor='w')
        tk.Label(banner, text='수동 낚시 모드 · 게임 창이 맨 앞일 때만 작동합니다.', bg='#172b4d', fg='#dbeafe').pack(anchor='w')
        modes = self.card('1   실행 모드')
        for label, value in [('인식만 확인 (입력 없음)', 'observe'), ('실제 실행', 'click')]:
            ttk.Radiobutton(modes, text=label, variable=self.mode, value=value, command=self.changed).pack(side='left', padx=(0,16))
        screen = self.card('2   화면 인식')
        self.preview = tk.Label(screen, text='시작하면 게임 화면이 표시됩니다.', bg='#eaf0f8', fg='#64748b', height=5)
        self.preview.pack(fill='x', pady=(0,8))
        ttk.Label(screen, textvariable=self.message, wraplength=730).pack(anchor='w')
        ttk.Label(screen, textvariable=self.state).pack(anchor='w', pady=4)
        ttk.Label(screen, textvariable=self.calibration, foreground='#64748b', wraplength=730).pack(anchor='w')
        row = ttk.Frame(screen); row.pack(fill='x', pady=(8,0))
        for key, title in [('message','문구 영역'), ('gauge','게이지 영역'), ('button','시작 버튼')]:
            ttk.Button(row, text=title+' 지정', command=lambda k=key: self.calibrate(k)).pack(side='left', padx=(0,6))
        rules = self.card('3   낚기 조건')
        grid = ttk.Frame(rules); grid.pack(fill='x')
        for i, key in enumerate(('heavy','lucky','float','bite')):
            ttk.Checkbutton(grid, text=PHRASES[key], variable=self.keep[key], command=self.changed).grid(row=i//2, column=i%2, sticky='w', padx=(0,40))
        ttk.Label(rules, text='반응이 왔다 / 뭔가 걸렸다    →    취소', foreground='#64748b').pack(anchor='w', pady=(8,0))
        settings = self.card('4   반복 설정')
        row = ttk.Frame(settings); row.pack(fill='x')
        ttk.Label(row,text='성공 횟수').pack(side='left'); ttk.Entry(row,textvariable=self.limit,width=5).pack(side='left',padx=8)
        ttk.Label(row,text='0 = 무제한').pack(side='left',padx=(0,20))
        ttk.Label(row,text='진행 없으면 정지').pack(side='left'); ttk.Entry(row,textvariable=self.timeout,width=5).pack(side='left',padx=8); ttk.Label(row,text='분').pack(side='left')
        row = ttk.Frame(settings); row.pack(fill='x',pady=8)
        ttk.Label(row,text='하급 취소 키').pack(side='left',padx=(0,8))
        ttk.Combobox(row,textvariable=self.cancel_key,values=('Esc','Space'),state='readonly',width=8).pack(side='left')
        ttk.Label(settings,text='취소 키는 PC 게임에서 확인 후 선택하세요. 낚기는 Space를 사용합니다.',foreground='#64748b',wraplength=730).pack(anchor='w')
        row = ttk.Frame(settings); row.pack(fill='x',pady=(10,0))
        ttk.Label(row,text='Tesseract 경로').pack(side='left',padx=(0,8))
        ttk.Entry(row,textvariable=self.tesseract).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='찾기',command=self.browse_ocr).pack(side='left',padx=(6,0))
        ttk.Label(settings,text='Tesseract 설치 필요 · 한국어 인식 데이터 포함 · 기본 경로 자동 검색',foreground='#64748b').pack(anchor='w',pady=4)
        ttk.Label(self.body,textvariable=self.counter,background=BG,font=('맑은 고딕',13,'bold')).pack(anchor='w',padx=24,pady=(0,12))
        self.load()

    def card(self, title):
        frame = tk.Frame(self.body,bg='white',highlightbackground='#dce3ee',highlightthickness=1,padx=16,pady=12)
        frame.pack(fill='x',padx=22,pady=(0,12))
        tk.Label(frame,text=title,bg='white',fg='#24334a',font=('맑은 고딕',15,'bold')).pack(anchor='w',pady=(0,8))
        return frame

    def changed(self):
        if self.app.running: self.app.stop('설정 변경 · 다시 시작하세요.')

    def browse_ocr(self):
        filename = filedialog.askopenfilename(filetypes=[('Tesseract', '*.exe')])
        if filename: self.changed(); self.tesseract.set(filename)

    def load(self):
        try:
            data = json.loads(self.config_path.read_text(encoding='utf-8'))
            for key, roi in data.get('rois',{}).items():
                if key in ('message','gauge','button') and len(roi)==4 and all(math.isfinite(v) and 0<=v<=1 for v in roi) and roi[2]>0 and roi[3]>0 and roi[0]+roi[2]<=1.001 and roi[1]+roi[3]<=1.001:
                    self.rois[key] = roi
            self.size = tuple(data['size']) if data.get('size') else None
            self.tesseract.set(data.get('tesseract',''))
            self.cancel_key.set(data.get('cancel_key','Esc'))
            for key, var in self.keep.items(): var.set(key in data.get('keep',list(KEEP)))
            path = self.home/'fishing_idle.png'
            if path.exists():
                self.template = np.array(Image.open(path).convert('RGB'))
        except (OSError, ValueError, TypeError, KeyError):
            logging.info('낚시 설정 없음 또는 읽기 실패')
        self.update_calibration()

    def save(self):
        data = {'rois':self.rois,'size':self.size,'tesseract':self.tesseract.get(),
                'cancel_key':self.cancel_key.get(),'keep':[k for k,v in self.keep.items() if v.get()]}
        self.config_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        if self.template is not None: Image.fromarray(self.template).save(self.home/'fishing_idle.png')

    def update_calibration(self):
        names = {'message':'문구','gauge':'게이지','button':'시작 버튼'}
        self.calibration.set('영역 설정: '+' / '.join(names[k]+(' ✓' if k in self.rois else ' 미지정') for k in names))

    def calibrate(self, key):
        self.app.stop('영역 지정 중 · 게임 화면을 준비하세요.')
        hints = {'message':'입질·결과 문구가 표시되는 부분만 좁게 선택하세요.',
                 'gauge':'입질 중 머리 위 게이지의 초록색과 빈 부분 전체를 선택하세요. 아이콘과 이름은 제외하세요.',
                 'button':'낚시를 중단한 상태의 일반 낚싯대 시작 버튼만 선택하세요. Space 글자는 제외하세요.'}
        messagebox.showinfo('영역 지정',hints[key]+'\n확인을 누르면 3초 후 게임 화면을 가져옵니다.')
        self.app.status.set('3초 후 화면 캡처 · 게임 창을 앞으로 가져오세요.')
        self.app.root.after(3000,lambda:self.capture_selection(key))

    def capture_selection(self, key):
        hwnd = self.app.find_game()
        if not hwnd: messagebox.showerror('게임 창','게임 창을 찾지 못했습니다.'); return
        if self.app.user32.GetForegroundWindow()!=hwnd:
            messagebox.showerror('게임 창','게임 창을 맨 앞으로 가져온 뒤 다시 지정하세요.'); return
        x,y,w,h = self.app.window_rect(hwnd)
        if w<640 or h<400: return
        rgb = np.array(ImageGrab.grab(bbox=(x,y,x+w,y+h),all_screens=True))
        if self.size and tuple(self.size)!=(w,h):
            self.rois.clear(); self.template=None
        self.size = (w,h)
        top = tk.Toplevel(self.app.root); top.title('드래그로 영역 선택 · Esc 취소')
        scale = min(1000/w,650/h,1.)
        iw,ih = int(w*scale),int(h*scale)
        photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((iw,ih)))
        canvas = tk.Canvas(top,width=iw,height=ih,cursor='crosshair');canvas.pack()
        canvas.create_image(0,0,image=photo,anchor='nw'); canvas.photo=photo
        origin = []; box = [None]
        def down(e):
            origin[:] = [max(0,min(iw,e.x)),max(0,min(ih,e.y))]
            if box[0]: canvas.delete(box[0])
            box[0] = canvas.create_rectangle(*origin,*origin,outline='#ff3030',width=2)
        def drag(e):
            if origin: canvas.coords(box[0],*origin,max(0,min(iw,e.x)),max(0,min(ih,e.y)))
        def up(e):
            if not origin: return
            x1,x2 = sorted((origin[0],max(0,min(iw,e.x))))
            y1,y2 = sorted((origin[1],max(0,min(ih,e.y))))
            if x2-x1<8 or y2-y1<4: return
            self.rois[key] = [x1/iw,y1/ih,(x2-x1)/iw,(y2-y1)/ih]
            if key=='button': self.template = crop(rgb,self.rois[key]).copy()
            self.save();self.update_calibration();top.destroy()
            self.app.status.set('영역 저장 완료 · 인식만 확인 모드로 먼저 확인하세요.')
        canvas.bind('<Button-1>',down); canvas.bind('<B1-Motion>',drag); canvas.bind('<ButtonRelease-1>',up)
        top.bind('<Escape>',lambda e:top.destroy()); top.grab_set()

    def start(self, size):
        if set(self.rois)!= {'message','gauge','button'} or self.template is None:
            raise ValueError('문구·게이지·시작 버튼 영역 3개를 먼저 지정하세요.')
        if tuple(size)!=tuple(self.size or ()):
            raise ValueError('게임 창 크기가 바뀌었습니다. 영역을 다시 지정하세요.')
        self.maxruns = int(self.limit.get()); self.wait = float(self.timeout.get())*60
        if self.maxruns<0 or not math.isfinite(self.wait) or self.wait<60:
            raise ValueError('성공 횟수는 0 이상, 정지 시간은 1분 이상이어야 합니다.')
        if self.cancel_key.get() not in ('Esc','Space'): raise ValueError('취소 키를 확인하세요.')
        self.ocr = KoreanOCR(self.tesseract.get().strip())
        self.save()
        self.run_mode = self.mode.get()
        self.run_cancel = self.cancel_key.get()
        self.engine.keep = {k for k,v in self.keep.items() if v.get()}
        self.engine.interrupted()
        self.epoch += 1
        self.clear_ocr()
        self.active = True
        self.focus_lost = False

    def clear_ocr(self):
        self.ocr_text = ''; self.ocr_phrase = self.ocr_result = None
        self.ocr_time = self.last_ocr = -100.
        self.last_phrase = None; self.phrase_stable = 0

    def stopped(self):
        self.active = False
        self.epoch += 1
        self.engine.interrupted()
        self.state.set('상태: 일시정지')

    def reset(self):
        self.engine.reset(); self.clear_ocr(); self.refresh_counts()
        self.state.set('상태: 중지됨'); self.message.set('문구: 대기 중')

    def refresh_counts(self):
        self.counter.set(f'성공 {self.engine.catches}회   취소 {self.engine.cancels}회   실패 {self.engine.failures}회')

    def lost_focus(self):
        if not self.focus_lost:
            self.engine.interrupted(); self.clear_ocr(); self.epoch += 1
            self.focus_lost = True
        self.state.set('상태: 게임 창 활성화 대기')

    def step(self, rgb, now, send_key):
        if (rgb.shape[1],rgb.shape[0])!=tuple(self.size):
            self.app.stop('게임 창 크기가 바뀌었습니다. 영역을 다시 지정하세요.'); return
        if self.focus_lost:
            self.focus_lost=False
            # Do not infer a rebound across a background interval.
            self.engine.interrupted()
        if self.future and self.future[0].done():
            job, epoch, started = self.future; self.future = None
            try: text=job.result()
            except Exception as exc:
                self.app.stop('한국어 OCR 오류: '+str(exc)); return
            if epoch==self.epoch and now-started<2.5:
                self.ocr_text=text; self.ocr_time=now
                phrase=classify(text)
                self.phrase_stable = self.phrase_stable+1 if phrase and phrase==self.last_phrase else 1
                self.last_phrase=phrase
                self.ocr_phrase=phrase if self.phrase_stable>=2 else None
                self.ocr_result=result_kind(text)
        if self.future is None and now-self.last_ocr>=.35:
            self.future=(self.executor.submit(self.ocr.read,crop(rgb,self.rois['message']).copy()),self.epoch,now)
            self.last_ocr=now
        level=gauge_level(crop(rgb,self.rois['gauge']))
        idle=idle_score(crop(rgb,self.rois['button']),self.template)>=.90
        fresh=now-self.ocr_time<1.5
        signal=Signal(level,self.ocr_phrase if fresh else None,idle,self.ocr_result if fresh else None)
        # Observation mode runs the same controller with virtual acknowledgements.
        action=self.engine.update(signal,now)
        if action=='stop':
            self.app.stop(self.engine.state+' · 게임 화면을 확인하세요.'); return
        self.message.set('문구: '+(PHRASES.get(self.engine.phrase) or self.ocr_text or '대기 중'))
        self.state.set('상태: '+STATE_NAMES.get(self.engine.state,self.engine.state)+(f' · 게이지 {level:.0%}' if level is not None else ''))
        self.refresh_counts()
        if self.maxruns and self.engine.catches>=self.maxruns:
            self.app.stop(f'낚시 {self.engine.catches}회 성공 · 목표 완료'); return
        if action:
            label={'cast':'낚시 시작','reel':'낚기','cancel':'하급 취소'}[action]
            if self.run_mode=='observe':
                # No virtual input: keep the proposed action visible while user plays.
                self.state.set('상태: 인식 확인 · '+label+' 입력 시점 (입력 없음)')
                if action in ('reel','cancel'):
                    self.engine.sent(action,now)
                    self.epoch+=1; self.clear_ocr()
            else:
                vk=0x1B if action=='cancel' and self.run_cancel=='Esc' else 0x20
                if not send_key(vk): return
                self.engine.sent(action,now)
                self.epoch+=1; self.clear_ocr()
                self.app.last_progress=now
                logging.info('fishing action=%s phrase=%s gauge=%s',action,self.engine.phrase,level)
            self.app.status.set(('인식 확인 · ' if self.run_mode=='observe' else '')+label)
        if now-self.preview_time < .15: return
        self.preview_time=now
        frame=rgb.copy()
        for key,roi in self.rois.items():
            x,y,w,h=roi; hh,ww=frame.shape[:2]
            cv2.rectangle(frame,(int(x*ww),int(y*hh)),(int((x+w)*ww),int((y+h)*hh)),(40,200,100) if key=='gauge' else (40,120,255),2)
        width=680; height=int(frame.shape[0]*width/frame.shape[1])
        self.photo=ImageTk.PhotoImage(Image.fromarray(frame).resize((width,height)))
        self.preview.configure(image=self.photo,text='',height=0)

    def close(self):
        self.executor.shutdown(wait=False,cancel_futures=True)
