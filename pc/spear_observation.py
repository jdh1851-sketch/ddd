"""Read-only native EntityMap OCR. No game-input or packet APIs.

Input must be original RGB window pixels. Image pixels never become world targets.
"""
from pathlib import Path
import sys,time
import cv2
import numpy as np
from spear_reading_rules import coordinate_consensus,parse_element

def coordinate_regions(rgb):
 h,w=rgb.shape[:2];mask=cv2.inRange(rgb,(145,145,145),(255,255,255));mask[:int(h*.70)]=0;mask[:,:int(w*.4)]=0;mask[:,int(w*.9):]=0
 connected=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((1,9),np.uint8))
 contours,_=cv2.findContours(connected,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);regions=[]
 for c in contours:
  x,y,rw,rh=cv2.boundingRect(c)
  if 30<=rw<=w*.5 and 4<=rh<=24 and 4<rw/rh<22:
   regions.append((max(0,x-3),max(0,y-3),min(w,x+rw+3),min(h,y+rh+3)))
 return regions

def label_regions(rgb):
 hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
 mask=((((hsv[:,:,0]<18)|(hsv[:,:,0]>166)) & (hsv[:,:,1]>65)&(hsv[:,:,2]>70)).astype('uint8')*255)
 points=cv2.findNonZero(mask)
 if points is None:return []
 origin_x,origin_y,rw,rh=cv2.boundingRect(points)
 cropped=mask[origin_y:origin_y+rh,origin_x:origin_x+rw]
 lines=cv2.HoughLinesP(cropped,1,np.pi/180,10,minLineLength=22,maxLineGap=5)
 if lines is None:return []
 horizontal=[]
 for x1,y1,x2,y2 in lines.reshape(-1,4):
  x1+=origin_x;x2+=origin_x;y1+=origin_y;y2+=origin_y
  if abs(int(y2)-int(y1))<=1:horizontal.append((min(int(x1),int(x2)),max(int(x1),int(x2)),round((int(y1)+int(y2))/2)))
 regions=[]
 for a in horizontal:
  for b in horizontal:
   if not 10<=b[2]-a[2]<=24:continue
   left=max(a[0],b[0]);right=min(a[1],b[1]);overlap=right-left
   if overlap<22 or overlap/min(a[1]-a[0]+1,b[1]-b[0]+1)<.65:continue
   box=(max(0,left-6),a[2]+2,min(rgb.shape[1],right+3),b[2]-1)
   if not any(np.linalg.norm(np.array(box)-np.array(q))<7 for q in regions):regions.append(box)
 # Partial rounded borders may expose only one horizontal segment.
 # Generate nearby text regions, then require the full semantic prefix from OCR.
 for left,right,y in horizontal:
  if right-left>90:continue
  for top,bottom in ((y+2,y+15),(y-14,y-1)):
   box=(max(0,left-2),max(0,top),min(rgb.shape[1],right+3),min(rgb.shape[0],bottom))
   if box[3]-box[1]>=7 and not any(np.linalg.norm(np.array(box)-np.array(q))<7 for q in regions):regions.append(box)
 expanded=[]
 for x,y,x2,y2 in regions[:24]:
  for box in ((x,y,x2,y2),(max(0,x-4),max(0,y-1),x2,y2),(x+2,y,min(rgb.shape[1],x2+15),y2),(x+2,y,min(rgb.shape[1],x2+13),y2),(max(0,x-4),max(0,y-1),min(rgb.shape[1],x2+15),y2)):
   if box[2]>box[0] and box[3]>box[1] and box not in expanded:expanded.append(box)
 return expanded


def adjacent_star(rgb,box,element):
 if element not in ('불','어둠'):return None
 x,y,x2,y2=box;height=y2-y;left=max(0,x-round(height*2.3));top=max(0,y-5);bottom=min(rgb.shape[0],y2+5)
 patch=rgb[top:bottom,left:x];hsv=cv2.cvtColor(patch,cv2.COLOR_RGB2HSV)
 color=(((hsv[:,:,0]<18)|(hsv[:,:,0]>166)) if element=='불' else ((hsv[:,:,0]>125)&(hsv[:,:,0]<166)))&(hsv[:,:,1]>90)&(hsv[:,:,2]>100)
 contours,_=cv2.findContours(color.astype('uint8')*255,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);stars=[]
 for c in contours:
  bx,by,w,h=cv2.boundingRect(c);area=cv2.contourArea(c);hull=cv2.contourArea(cv2.convexHull(c))
  polygon=cv2.approxPolyDP(c,.025*cv2.arcLength(c,True),True)
  if 7<=w<=height*1.6 and 7<=h<=height*1.6 and area>20 and hull and .35<area/hull<.87 and 7<=len(polygon)<=16:
   m=cv2.moments(c)
   if m['m00']:stars.append({'box':[left+bx,top+by,w,h],'pixel':[round(left+m['m10']/m['m00'],1),round(top+m['m01']/m['m00'],1)],'role':'answer-star candidate'})
 return stars[0] if len(stars)==1 else None


class TesseractTextReader:
 def __init__(self, data_dir=None):
  from fishing import KoreanOCR
  self.ocr=KoreanOCR()
  self.data_dir=Path(data_dir) if data_dir else Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'tessdata'
  if not all((self.data_dir/f'{lang}.traineddata').is_file() for lang in ('kor','eng')):
   raise ValueError('시험판의 한국어·숫자 OCR 데이터가 없습니다.')

 def __call__(self, patch,lang,psm,variant='rgb'):
  if variant=='inverted':patch=255-cv2.cvtColor(patch,cv2.COLOR_RGB2GRAY)
  elif variant=='gray':patch=cv2.cvtColor(patch,cv2.COLOR_RGB2GRAY)
  value=255 if patch.ndim==2 else (255,255,255)
  patch=cv2.copyMakeBorder(patch,5,5,5,5,cv2.BORDER_CONSTANT,value=value)
  patch=cv2.resize(patch,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
  # Explicit argument list preserves Windows paths with spaces without passing
  # literal quote characters through pytesseract's config parser.
  import io,subprocess
  from PIL import Image
  stream=io.BytesIO();Image.fromarray(patch).save(stream,format='PNG')
  command=[self.ocr.api.pytesseract.tesseract_cmd,'stdin','stdout',
           '--tessdata-dir',str(self.data_dir),'-l',lang,'--psm',str(psm)]
  if lang=='eng':command+=['-c','tessedit_char_whitelist=XYxy0123456789.,:-+']
  try:
   proc=subprocess.run(command,input=stream.getvalue(),capture_output=True,
                       timeout=1.2,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  except subprocess.TimeoutExpired:return ''
  if proc.returncode:raise ValueError('OCR 실행 실패: '+proc.stderr.decode('utf-8',errors='replace')[:300])
  return proc.stdout.decode('utf-8',errors='replace').strip()



def canonical_label(records,prefix):
 candidates=[r for r in records if r['prefix']==prefix]
 if not candidates or len({r['element'] for r in candidates})!=1:return None
 groups=[]
 for r in candidates:
  x,y,x2,y2=r['box'];center=np.array([(x+x2)/2,(y+y2)/2])
  for group in groups:
   a,b,c,d=group[0]['box']
   if np.linalg.norm(center-np.array([(a+c)/2,(b+d)/2]))<14:
    group.append(r);break
  else:groups.append([r])
 if len(groups)!=1:return None
 return min(groups[0],key=lambda r:(r['box'][2]-r['box'][0])*(r['box'][3]-r['box'][1]))


class NativeMapReader:
 def __init__(self, text_reader=None):
  self.text_reader=text_reader
  self.reset()

 def reset(self):
  self.references={}

 def cached_regions(self,rgb):
  found=[]
  for template in self.references.values():
   h,w=template.shape[:2]
   if h>rgb.shape[0] or w>rgb.shape[1]:continue
   scores=cv2.matchTemplate(rgb,template,cv2.TM_CCOEFF_NORMED)
   _,score,_,(x,y)=cv2.minMaxLoc(scores)
   if score>=.70:found.append((x,y,x+w,y+h))
  return found

 def read(self,rgb,budget_seconds=6.):
  if rgb.ndim!=3 or rgb.shape[2]!=3 or rgb.shape[0]<60 or rgb.shape[1]<80:
   raise ValueError('원본 RGB 좌표 창 이미지를 확인하세요.')
  started=time.monotonic();deadline=started+budget_seconds
  if self.text_reader is None:self.text_reader=TesseractTextReader()
  coords=[];labels=[];examined=0;partial=False;attempts=[]
  for box in coordinate_regions(rgb):
   if time.monotonic()>=deadline:partial=True;break
   x,y,x2,y2=box;patch=rgb[y:y2,x:x2]
   texts=[self.text_reader(patch,'eng',7,v) for v in ('rgb','gray')]
   attempts.append({'kind':'coordinates','box':box,'texts':[s[:100] for s in texts]})
   value=coordinate_consensus(texts)
   if value is not None:coords.append({'box':box,'value':value})
  regions=self.cached_regions(rgb)+label_regions(rgb)
  seen=set()
  for box in regions:
   if time.monotonic()>=deadline:partial=True;break
   if box in seen:continue
   seen.add(box);examined+=1;x,y,x2,y2=box;patch=rgb[y:y2,x:x2]
   raw=self.text_reader(patch,'kor',7,'inverted')
   attempt={'kind':'label','box':box,'psm7':raw[:100]};attempts.append(attempt)
   for prefix in ('정답','투창병'):
    element=parse_element(raw,prefix)
    if element is None:continue
    check=self.text_reader(patch,'kor',6,'inverted')
    attempt['psm6']=check[:100]
    if parse_element(check,prefix)!=element:continue
    labels.append({'box':box,'prefix':prefix,'element':element})
   boss=canonical_label(labels,'투창병');answer=canonical_label(labels,'정답')
   if boss and answer and boss['element']==answer['element']:break
  else:
   boss=canonical_label(labels,'투창병');answer=canonical_label(labels,'정답')
  # Recompute after a deadline exit; never retain a previous frame's text.
  boss=canonical_label(labels,'투창병');answer=canonical_label(labels,'정답')
  element=boss['element'] if boss and answer and boss['element']==answer['element'] else None
  star=adjacent_star(rgb,answer['box'],element) if element else None
  if element:
   for r in (boss,answer):
    x,y,x2,y2=r['box'];self.references[r['prefix']]=rgb[y:y2,x:x2].copy()
  else:self.references={}
  values={r['value'] for r in coords}
  return {'coordinates':next(iter(values)) if len(values)==1 else None,
          'coordinate_regions':coords,'boss':boss,'answer':answer,'element':element,
          'star':star,'partial':partial,'regions_examined':examined,
          'ocr_attempts':attempts,
          'processing_seconds':time.monotonic()-started,
          'movement_allowed':False,'success_confirmed':False,
          'self_target_confirmed':False,'target_world_coordinates':None}
