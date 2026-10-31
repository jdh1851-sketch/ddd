import ctypes as C
from ctypes import wintypes as W
import sys, time, json, pathlib, logging
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import ImageGrab, Image, ImageTk
from fishing_ui import FishingPanel
from scan_async import LatestScan

BASE = pathlib.Path(getattr(sys, '_MEIPASS', pathlib.Path(__file__).parent))
HOME = pathlib.Path(sys.executable).parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).parent
ASSETS = BASE if (BASE/'templates').exists() else BASE.parent/'assets'
u = C.windll.user32
try: C.windll.shcore.SetProcessDpiAwareness(2)
except Exception: u.SetProcessDPIAware()
u.GetForegroundWindow.restype = W.HWND
u.GetAncestor.argtypes = [W.HWND, W.UINT]
u.GetAncestor.restype = W.HWND
u.WindowFromPoint.argtypes = [W.POINT]
u.WindowFromPoint.restype = W.HWND
logging.basicConfig(filename=str(HOME / 'abyss.log'), level=logging.INFO, encoding='utf-8', format='%(asctime)s %(message)s')

NAMES = {'enter':'입장하기', 'compass':'나침반', 'skip':'장면 넘기기', 'touch':'화면 터치', 'retry':'다시 하기', 'food':'음식 B', 'break':'브레이크', 'partyready':'준비 완료', 'partycancel':'준비 취소'}
ALLOWED = {'ready':['partyready','enter','compass'], 'battle':['skip','touch'], 'result':['retry'], 'queue':['partyready','compass','enter']}

def game_window():
    found=[]
    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def cb(h, p):
        n=u.GetWindowTextLengthW(h); b=C.create_unicode_buffer(n+1); u.GetWindowTextW(h,b,n+1)
        if b.value.strip() in ('마비노기 모바일','Mabinogi Mobile','MabinogiMobile') and u.IsWindowVisible(h): found.append(h)
        return True
    u.EnumWindows(cb,0)
    return found[0] if found else None

def rect(h):
    r=W.RECT(); u.GetClientRect(h,C.byref(r)); p=W.POINT(0,0); u.ClientToScreen(h,C.byref(p))
    return p.x,p.y,r.right,r.bottom

class Detector:
    def __init__(self):
        self.cfg=json.loads((ASSETS/'templates/config.json').read_text(encoding='utf-8'))
        self.templates={k:cv2.imdecode(np.fromfile(str(ASSETS/'templates'/f'{k}.png'),dtype=np.uint8),0) for k in self.cfg}
        self.variants={k:[v] for k,v in self.templates.items()}
        for k in ('touch','compass'):
            self.variants[k]=[cv2.imdecode(np.fromfile(str(p),dtype=np.uint8),0) for p in sorted((ASSETS/'templates').glob(k+'*.png'))]
        v=cv2.threshold(self.templates['compass'],210,255,cv2.THRESH_BINARY)[1]
        hh,ww=v.shape
        self.variants['compass']=[cv2.warpAffine(v,cv2.getRotationMatrix2D((ww/2,hh/2),a,1),(ww,hh)) for a in range(0,360,15)]
        self.variants['break']=[cv2.threshold(self.templates['break'],210,255,cv2.THRESH_BINARY)[1]]
    def capture(self, bbox, enabled):
        return self.scan(ImageGrab.grab(bbox=bbox,all_screens=True),enabled)
    def scan(self, rgb, enabled=None):
        frame=cv2.resize(np.array(rgb),(1660,1024))
        gray=cv2.cvtColor(frame,cv2.COLOR_RGB2GRAY)
        hits={}
        for k,cfg in self.cfg.items():
            if enabled is not None and k not in enabled:continue
            x,y,w,h=cfg['roi']; patch=gray[y:y+h,x:x+w]
            if k in ('compass','break'):patch=cv2.threshold(patch,210,255,cv2.THRESH_BINARY)[1]
            score=-1; pos=None
            for variant in self.variants[k]:
                for scale in ((.75,.85,.94,1.,1.06,1.15,1.3) if k=='break' else (.94,1.,1.06)):
                    t=cv2.resize(variant,None,fx=scale,fy=scale)
                    if t.shape[0]>h or t.shape[1]>w: continue
                    _,v,_,loc=cv2.minMaxLoc(cv2.matchTemplate(patch,t,cv2.TM_CCOEFF_NORMED))
                    if v>score: score=v;pos=(x+loc[0]+t.shape[1]/2,y+loc[1]+t.shape[0]/2)
            hits[k]=(score,pos)
        return frame,hits

class App:
    def __init__(self):
        self.root=tk.Tk();self.root.title('끌림 매크로 v0.10');self.root.iconbitmap(str(BASE/'app_icon.ico'))
        self.user32=u;self.find_game=game_window;self.window_rect=rect
        self.root.geometry(f'1000x{min(1100,self.root.winfo_screenheight()-100)}');self.root.minsize(820,650)
        self.root.configure(bg='#f2f5fa');self.root.option_add('*Font',('맑은 고딕',12))
        style=ttk.Style();style.theme_use('clam')
        style.configure('.',font=('맑은 고딕',12),background='#ffffff',foreground='#24334a')
        style.configure('TFrame',background='#ffffff')
        style.configure('TLabel',background='#ffffff',foreground='#24334a')
        style.configure('TRadiobutton',font=('맑은 고딕',13),background='#ffffff',padding=6)
        style.configure('TCheckbutton',font=('맑은 고딕',13),background='#ffffff',padding=8)
        style.configure('TEntry',padding=6)
        style.configure('TButton',font=('맑은 고딕',12,'bold'),padding=(12,12))
        style.configure('TNotebook.Tab',font=('맑은 고딕',14,'bold'),padding=(24,12))
        style.map('TNotebook.Tab',background=[('selected','#dbeafe')],foreground=[('selected','#1764dc')])
        style.configure('Start.TButton',background='#2866d8',foreground='white')
        style.map('Start.TButton',background=[('active','#2056b9')])
        style.configure('Stop.TButton',background='#be384b',foreground='white')
        style.map('Stop.TButton',background=[('active','#a52d40')])
        self.vision=LatestScan();self.scan_after=0
        self.det=Detector();self.running=False;self.state='ready';self.count=0;self.pending=None;self.last_progress=time.monotonic();self.delay=0
        self.mode=tk.StringVar(value='observe');self.limit=tk.StringVar(value='1');self.threshold=tk.StringVar(value='0.84');self.timeout=tk.StringVar(value='15')
        self.status=tk.StringVar(value='중지됨 · 게임을 준비한 뒤 시작해 주세요.')
        # Keep stop/start accessible even when content must scroll on a small display.
        footer=tk.Frame(self.root,bg='#f2f5fa');footer.pack(side='bottom',fill='x',padx=22,pady=(6,14))
        actions=tk.Frame(footer,bg='#f2f5fa');actions.pack(fill='x')
        ttk.Button(actions,text='시작 / 일시정지  F8',style='Start.TButton',command=self.toggle).pack(side='left',fill='x',expand=True,padx=(0,8))
        ttk.Button(actions,text='즉시정지  F9',style='Stop.TButton',command=lambda:self.stop('사용자 정지')).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='회차 초기화',command=self.reset).pack(side='left')
        tk.Label(footer,textvariable=self.status,bg='#f2f5fa',fg='#52637c',anchor='w',justify='left',wraplength=810,font=('맑은 고딕',11)).pack(fill='x',pady=(9,0))
        self.tabs=ttk.Notebook(self.root);self.tabs.pack(fill='both',expand=True)
        abyss_page=ttk.Frame(self.tabs);fishing_page=ttk.Frame(self.tabs)
        self.tabs.add(abyss_page,text='  어비스  ');self.tabs.add(fishing_page,text='  낚시  ')
        self.fishing=FishingPanel(fishing_page,self,HOME)
        self.tabs.bind('<<NotebookTabChanged>>',self.tab_changed)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        canvas=tk.Canvas(abyss_page,bg='#f2f5fa',highlightthickness=0)
        self.abyss_canvas=canvas
        scroll=ttk.Scrollbar(abyss_page,orient='vertical',command=canvas.yview);scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body=tk.Frame(canvas,bg='#f2f5fa');window=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        self.root.bind('<MouseWheel>',self.scroll_page)
        tk.Label(body,text='끌림 매크로',font=('맑은 고딕',24,'bold'),bg='#f2f5fa',fg='#24334a').pack(anchor='w',padx=24,pady=(16,2))
        tk.Label(body,text='우연한 만남 · 파티 입장 및 재도전',font=('맑은 고딕',12),bg='#f2f5fa',fg='#64748b').pack(anchor='w',padx=26,pady=(0,14))
        banner=tk.Frame(body,bg='#172b4d',padx=18,pady=13);banner.pack(fill='x',padx=22)
        tk.Label(banner,text='F8  시작 / 일시정지       F9  즉시정지',font=('맑은 고딕',17,'bold'),bg='#172b4d',fg='white').pack(anchor='w')
        tk.Label(banner,text='게임 창이 맨 앞일 때만 작동합니다.',font=('맑은 고딕',13,'bold'),bg='#172b4d',fg='#dbeafe').pack(anchor='w',pady=(5,0))
        tk.Label(body,text='입장하기 · 준비 완료 자동 처리  /  준비 취소는 대기',font=('맑은 고딕',12,'bold'),bg='#f2f5fa',fg='#36557d').pack(anchor='w',padx=26,pady=12)
        def card(title):
            f=tk.Frame(body,bg='white',highlightbackground='#dce3ee',highlightthickness=1,padx=16,pady=12);f.pack(fill='x',padx=22,pady=(0,12))
            tk.Label(f,text=title,font=('맑은 고딕',15,'bold'),bg='white',fg='#24334a').pack(anchor='w',pady=(0,8))
            return f
        modes=card('1   실행 모드 선택')
        row=ttk.Frame(modes);row.pack(fill='x')
        ttk.Radiobutton(row,text='인식만 확인 (클릭 없음)',variable=self.mode,value='observe').pack(side='left',padx=(0,20))
        ttk.Radiobutton(row,text='실제 실행',variable=self.mode,value='click').pack(side='left')
        tk.Label(modes,text='미리보기 · 인식된 대상에 사각형 표시',font=('맑은 고딕',11),bg='white',fg='#64748b').pack(anchor='w',pady=(8,6))
        self.preview=tk.Label(modes,text='시작하면 이곳에 게임 화면이 표시됩니다.',font=('맑은 고딕',12),bg='#eaf0f8',fg='#64748b',width=56,height=3);self.preview.pack(pady=(0,2))
        options=card('2   입력 및 선택 기능')
        self.inputmode=tk.StringVar(value='space')
        row=ttk.Frame(options);row.pack(fill='x')
        ttk.Label(row,text='기본 입력').pack(side='left',padx=(0,25))
        ttk.Radiobutton(row,text='Space 키',variable=self.inputmode,value='space').pack(side='left')
        ttk.Radiobutton(row,text='마우스',variable=self.inputmode,value='mouse').pack(side='left')
        self.option_states=[]
        def clear_checkbox(label,var):
            row=tk.Frame(options,bg='white',cursor='hand2');row.pack(fill='x',pady=5)
            mark=tk.Canvas(row,width=32,height=34,bg='white',highlightthickness=0,takefocus=1);mark.pack(side='left',padx=(2,8))
            caption=tk.Label(row,text=label,font=('맑은 고딕',13),bg='white',fg='#24334a',anchor='w');caption.pack(side='left')
            state=tk.Label(row,font=('맑은 고딕',11,'bold'),padx=10,pady=5);state.pack(side='right',padx=(8,0))
            def refresh(*args):
                selected=var.get();mark.delete('all')
                mark.create_rectangle(3,5,27,29,fill='#2866d8' if selected else 'white',outline='#2866d8' if selected else '#9aa8bc',width=2)
                if selected:mark.create_line(8,17,13,23,23,11,fill='white',width=3,capstyle='round',joinstyle='round')
                state.configure(text='사용 중' if selected else '사용 안 함',fg='#245cc0' if selected else '#64748b',bg='#eaf1ff' if selected else '#f1f4f8')
            def toggle_option(event=None):
                var.set(not var.get());mark.focus_set();return 'break'
            for widget in (row,mark,caption,state):widget.bind('<Button-1>',toggle_option)
            mark.bind('<space>',toggle_option);mark.bind('<Return>',toggle_option)
            mark.bind('<FocusIn>',lambda e:mark.configure(highlightthickness=1,highlightbackground='#2866d8',highlightcolor='#2866d8'))
            mark.bind('<FocusOut>',lambda e:mark.configure(highlightthickness=0))
            var.trace_add('write',refresh);refresh();self.option_states.append((var,state))
        self.food=tk.BooleanVar(value=False);self.food_used=False;self.food_seen=0;self.food_absent=0;self.food_last=0
        clear_checkbox('음식 B 버튼이 나타나면 음식 먹기',self.food)
        self.break_skill=tk.BooleanVar(value=False);self.break_used=False;self.break_absent=0;self.break_last=0
        clear_checkbox('브레이크 / 익스텐드 감지 시 6번 스킬 사용',self.break_skill)
        settings=card('3   반복 설정')
        row=ttk.Frame(settings);row.pack(fill='x')
        ttk.Label(row,text='완료 횟수').pack(side='left');ttk.Entry(row,textvariable=self.limit,width=5).pack(side='left',padx=8)
        ttk.Label(row,text='0 = 무제한',foreground='#64748b').pack(side='left',padx=(0,22))
        ttk.Label(row,text='진행 없으면 정지').pack(side='left');ttk.Entry(row,textvariable=self.timeout,width=5).pack(side='left',padx=8);ttk.Label(row,text='분').pack(side='left')
        row=ttk.Frame(settings);row.pack(fill='x',pady=(10,0))
        ttk.Label(row,text='인식 기준').pack(side='left');ttk.Entry(row,textvariable=self.threshold,width=6).pack(side='left',padx=8)
        ttk.Label(row,text='기본 0.84 · 인식 확인은 첫 사용 시 권장',foreground='#64748b',font=('맑은 고딕',10)).pack(side='left')
        self.keys={};self.candidate=None;self.stable=0
        self.root.after(250,self.tick)
    def scroll_page(self,event):
        canvas=self.fishing.canvas if self.is_fishing() else self.abyss_canvas
        canvas.yview_scroll(-int(event.delta/120),'units')
    def is_fishing(self):
        return self.tabs.index(self.tabs.select())==1
    def tab_changed(self,event=None):
        if self.running:self.stop('탭 변경 · 일시정지')
    def close(self):
        self.stop('종료');self.vision.close();self.fishing.close();self.root.destroy()
    def reset(self):
        if self.running:return
        if self.is_fishing():
            self.fishing.reset();self.status.set('낚시 회차 초기화 완료.');return
        self.state='ready';self.count=0;self.pending=None;self.candidate=None;self.stable=0;self.food_used=False
        self.status.set('회차 초기화 완료. 입장 화면 또는 던전 시작 화면에서 시작하세요.')
    def stop(self,msg):
        self.running=False;self.vision.reset();self.fishing.stopped();self.status.set(msg);logging.info(msg)
    def toggle(self):
        if self.running:self.stop('일시정지');return
        if self.is_fishing():
            self.hwnd=game_window()
            if not self.hwnd:messagebox.showerror('게임 창','마비노기 모바일 창을 찾지 못했습니다.');return
            try:self.fishing.start(rect(self.hwnd)[2:])
            except (ValueError, ImportError, OSError) as exc:
                messagebox.showerror('낚시 설정',str(exc));return
            self.wait=self.fishing.wait
            self.running=True;self.last_progress=time.monotonic();self.delay=self.last_progress+3
            self.status.set('3초 후 낚시 시작 · 게임 창을 맨 앞으로 가져오세요.');return
        try:
            self.maxruns=int(self.limit.get());self.th=float(self.threshold.get());self.wait=float(self.timeout.get())*60
            if self.maxruns<0 or not .6<=self.th<=.99 or self.wait<60:raise ValueError()
        except ValueError:messagebox.showerror('설정','횟수 0 이상, 인식 기준 0.60~0.99, 정지 시간 1분 이상을 입력하세요.');return
        self.hwnd=game_window()
        if not self.hwnd:messagebox.showerror('게임 창','마비노기 모바일 창을 찾지 못했습니다. 게임을 실행해 주세요.');return
        if self.pending:
            self.state={'enter':'ready','compass':'queue','skip':'battle','touch':'battle','retry':'result','partyready':'queue'}[self.pending[0]]
            self.pending=None
        self.running=True;self.last_progress=time.monotonic();self.delay=time.monotonic()+3
        self.status.set('3초 후 시작합니다. 게임 창을 클릭해 맨 앞으로 가져오세요.')
    def hotkeys(self):
        for vk,fn in [(0x77,self.toggle),(0x78,lambda:self.stop('F9 즉시정지'))]:
            down=bool(u.GetAsyncKeyState(vk)&0x8000)
            if down and not self.keys.get(vk):fn()
            self.keys[vk]=down
    def tick(self):
        try:
            self.hotkeys()
            if self.running:self.step()
        except Exception as e:
            logging.exception('실행 오류');self.stop('오류로 정지: '+str(e))
        self.root.after(50,self.tick)
    def fishing_key(self,vk):
        if not self.running or not self.is_fishing() or u.GetForegroundWindow()!=self.hwnd or u.IsIconic(self.hwnd):return False
        scan=0x01 if vk==0x1B else 0x39
        u.keybd_event(vk,scan,0,0)
        try:time.sleep(.04)
        finally:u.keybd_event(vk,scan,2,0)
        return True
    def step(self):
        now=time.monotonic()
        if now<self.delay:return
        if not u.IsWindow(self.hwnd):self.stop('게임 창이 닫혀 정지했습니다.');return
        if now-self.last_progress>self.wait:self.stop('진행 제한 시간을 초과해 정지했습니다. 화면을 확인하세요.');return
        if u.GetForegroundWindow()!=self.hwnd or u.IsIconic(self.hwnd):
            self.vision.reset()
            if self.is_fishing():self.fishing.lost_focus()
            self.status.set('대기: 게임 창을 맨 앞으로 가져오세요.');return
        x,y,w,h=rect(self.hwnd)
        if w<640 or h<400:self.stop('게임 창이 너무 작습니다.');return
        if self.is_fishing():
            rgb=ImageGrab.grab(bbox=(x,y,x+w,y+h),all_screens=True)
            self.fishing.step(np.array(rgb),now,self.fishing_key);return
        result=self.vision.poll()
        if result is None:
            if now>=self.scan_after:
                enabled=set(self.det.cfg)
                if not self.food.get():enabled.discard('food')
                if not self.break_skill.get():enabled.discard('break')
                if self.vision.submit(self.det.capture,(x,y,x+w,y+h),enabled):self.scan_after=now+.35
            return
        frame,hits=result
        valid={k:v for k,v in hits.items() if v[0]>=self.th}
        for k,(score,pos) in valid.items():
            px,py=map(int,pos);cv2.rectangle(frame,(px-45,py-20),(px+45,py+20),(255,80,0),3)
        self.photo=ImageTk.PhotoImage(Image.fromarray(frame).resize((460,284)));self.preview.configure(image=self.photo,text='',width=0,height=0)
        scores=' / '.join(f'{NAMES[k]} {v[0]:.2f}' for k,v in hits.items())
        if self.mode.get()=='observe':
            self.status.set('인식 확인 · 클릭 없음\n'+scores);return
        # A party confirmation can overlay the entry/result screen.
        # Never send Space while the player is already marked ready.
        if 'partycancel' in valid:
            if self.pending:self.pending=None;self.last_progress=now
            self.state='queue';self.candidate=None;self.stable=0
            self.status.set('준비 완료 상태 · 다른 파티원과 입장을 기다립니다.')
            return
        if 'partyready' in valid and self.pending and self.pending[0] in ('enter','retry'):
            self.pending=None;self.state='queue';self.last_progress=now
        if 'break' not in valid:
            self.break_absent+=1
            if self.break_absent>=2:self.break_used=False
        else:self.break_absent=0
        if self.break_skill.get() and self.state=='battle' and not self.pending and 'break' in valid and not self.break_used and now-self.break_last>2:
            if u.GetForegroundWindow()!=self.hwnd:return
            u.keybd_event(0x36,0x07,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x36,0x07,2,0)
            self.break_used=True;self.break_last=now
            logging.info('input=key_6 action=break_skill score=%.3f',hits['break'][0])
            self.status.set('브레이크 표시 감지 → 숫자 6 입력')
            return
        # Re-arm only after the food prompt disappears for several frames.
        if 'food' in valid:
            self.food_seen+=1;self.food_absent=0
        else:
            self.food_seen=0;self.food_absent+=1
            if self.food_absent>=4:self.food_used=False
        if self.food.get() and self.state in ('queue','battle') and not self.pending and self.food_seen>=2 and not self.food_used and now-self.food_last>10:
            if u.GetForegroundWindow()!=self.hwnd:return
            u.keybd_event(0x42,0x30,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x42,0x30,2,0)
            self.food_used=True;self.food_last=now;self.delay=time.monotonic()+1
            logging.info('input=key_B action=food score=%.3f',hits['food'][0])
            self.status.set('음식 B 버튼 감지 → B 입력. 버튼이 사라질 때까지 재입력하지 않습니다.')
            return
        if self.pending:
            key,when=self.pending
            if key not in valid:
                self.pending=None;self.last_progress=now
            elif now-when>30:self.stop(NAMES[key]+' 입력 후 화면 전환이 확인되지 않아 정지했습니다.');return
            else:return
        candidates=[k for k in ALLOWED[self.state] if k in valid]
        key=candidates[0] if candidates else None
        self.stable=self.stable+1 if key and key==self.candidate else 1
        self.candidate=key
        self.status.set(f'상태: {self.state} · 완료 {self.count}회\n'+scores)
        if not key or self.stable<2:return
        if key=='retry' and self.maxruns and self.count+1>=self.maxruns:
            self.count+=1
            self.stop(f'{self.count}회 완료. 결과 화면에서 정지했습니다.');return
        px,py=hits[key][1]
        if key=='touch':px,py=830,560
        sx,sy=x+int(px*w/1660),y+int(py*h/1024)
        # Recheck ownership immediately before sending mouse input.
        if u.GetForegroundWindow()!=self.hwnd:return
        if u.GetAncestor(u.WindowFromPoint(W.POINT(sx,sy)),2)!=self.hwnd:return
        method='space' if self.inputmode.get()=='space' and key in ('enter','compass','retry','partyready') else 'mouse'
        if method=='space':
            u.keybd_event(0x20,0x39,0,0)
            try:time.sleep(.12)
            finally:u.keybd_event(0x20,0x39,2,0)
        else:
            u.SetCursorPos(sx,sy);time.sleep(.06)
            u.mouse_event(2,0,0,0,0)
            try:time.sleep(.12)
            finally:u.mouse_event(4,0,0,0,0)
        if key=='retry':self.count+=1
        logging.info('input=%s action=%s score=%.3f point=(%d,%d) count=%d',method,key,hits[key][0],sx,sy,self.count)
        self.state={'enter':'queue','compass':'battle','skip':'battle','touch':'result','retry':'queue','partyready':'queue'}[key]
        self.pending=(key,now);self.delay=now+1.2;self.last_progress=now;self.stable=0

if __name__=='__main__':
    try:
        app=App()
        if '--selftest' in sys.argv:
            app.root.geometry('1000x740+10+10');app.root.update()
            from fishing import KoreanOCR
            packaged_ocr=KoreanOCR()
            assert packaged_ocr.api.get_tesseract_version()
            assert len(app.tabs.tabs())==2
            from threading import Event
            busy=Event();app.vision.submit(lambda:busy.wait(5))
            timings=[]
            try:
                for index in (1,0,1,0):
                    started=time.perf_counter();app.tabs.select(index);app.root.update()
                    timings.append((time.perf_counter()-started)*1000)
                assert max(timings)<750, f'Tab switching blocked: {timings}'
                assert not app.vision.future[0].done(), 'Simulated vision task must still be busy'
            finally:busy.set()
            assert all(v.get() for v in app.fishing.keep.values())
            (HOME/'selftest-ui.json').write_text(json.dumps({'tab_switch_ms':timings,'vision_busy_during_switch':True}),encoding='utf-8')
            for index,name in ((1,'fishing'),(0,'abyss')):
                app.tabs.select(index);app.root.update();time.sleep(.15);app.root.update()
                x,y=app.root.winfo_rootx(),app.root.winfo_rooty()
                ImageGrab.grab(bbox=(x,y,x+app.root.winfo_width(),y+app.root.winfo_height())).save(HOME/f'preview-{name}.png')
            for var,label in app.option_states:
                assert not var.get() and label.cget('text')=='사용 안 함'
                var.set(True);assert label.cget('text')=='사용 중'
                var.set(False);assert label.cget('text')=='사용 안 함'
            (HOME/'selftest.txt').write_text('OK: GUI tabs, bundled OCR runtime, OpenCV, templates, Windows API initialized. No game input sent.',encoding='utf-8')
            app.vision.close();app.fishing.close();app.root.destroy()
        else:app.root.mainloop()
    except Exception:
        logging.exception('프로그램 초기화 오류')
        if '--selftest' in sys.argv:sys.exit(1)
        raise
