"""Fetch redistribution assets with fixed checksums; never download at app startup."""
from pathlib import Path
import hashlib
import urllib.request

root=Path(__file__).resolve().parents[1]/'tessdata'
root.mkdir(exist_ok=True)
expected='6b85e11d9bbf07863b97b3523b1b112844c43e713df8b66418a081fd1060b3b2'
model=root/'kor.traineddata'
if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest()!=expected:
    data=urllib.request.urlopen('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/kor.traineddata',timeout=60).read()
    if hashlib.sha256(data).hexdigest()!=expected:
        raise RuntimeError('Korean model checksum changed; review before updating the expected hash.')
    model.write_bytes(data)
if not (root/'LICENSE').exists():
    data=urllib.request.urlopen('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/LICENSE',timeout=60).read()
    (root/'LICENSE').write_bytes(data)
print('Korean OCR data verified.')
expected_eng='7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
english=root/'eng.traineddata'
if not english.exists() or hashlib.sha256(english.read_bytes()).hexdigest()!=expected_eng:
    data=urllib.request.urlopen('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata',timeout=60).read()
    if hashlib.sha256(data).hexdigest()!=expected_eng:
        raise RuntimeError('English model checksum changed; review before updating the expected hash.')
    english.write_bytes(data)
print('Coordinate OCR data verified.')
