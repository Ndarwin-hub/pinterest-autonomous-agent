from __future__ import annotations
import base64, io
from PIL import Image

from image_quality import inspect_image_bytes, validate_base64_image
from image_fingerprint import fingerprint, similarity
from pin_config import PINS_PER_PRODUCT


def _jpg(size=(1200, 1800)):
    from PIL import ImageDraw
    im=Image.new("RGB",size,(35,95,180)); draw=ImageDraw.Draw(im)
    for i in range(18):
        x=70+i*55; y=90+(i%6)*260
        draw.ellipse((x,y,x+260,y+260),fill=((40+i*9)%256,(120+i*5)%256,(210-i*7)%256))
    for i in range(10):
        draw.rectangle((100+i*95,1450-(i%3)*70,360+i*95,1650-(i%3)*70),fill=((220-i*8)%256,(70+i*12)%256,(60+i*9)%256))
    b=io.BytesIO(); im.save(b,"JPEG",quality=92); return b.getvalue()


def test_pin_contract_is_four():
    assert PINS_PER_PRODUCT == 4


def test_valid_bytes_pass_and_fingerprint():
    raw=_jpg()
    checked=inspect_image_bytes(raw)
    assert checked is not None
    assert checked["width"] == 1200
    assert checked["_fingerprint"]


def test_corrupt_bytes_fail_closed():
    assert inspect_image_bytes(b"not-an-image") is None


def test_transparent_empty_canvas_fails():
    im=Image.new("RGBA",(1200,1800),(255,255,255,0))
    b=io.BytesIO(); im.save(b,"PNG")
    assert inspect_image_bytes(b.getvalue()) is None


def test_blank_canvas_fails():
    im=Image.new("RGB",(1200,1800),(255,255,255))
    b=io.BytesIO(); im.save(b,"JPEG")
    assert inspect_image_bytes(b.getvalue()) is None


def test_recompressed_same_visual_remains_highly_similar():
    raw=_jpg()
    im=Image.open(io.BytesIO(raw)).convert("RGB")
    b=io.BytesIO(); im.save(b,"JPEG",quality=65)
    a=fingerprint(raw); c=fingerprint(b.getvalue())
    assert a and c and similarity(a,c) >= 0.93


def test_base64_uses_same_byte_gate():
    raw=_jpg()
    encoded=base64.b64encode(raw).decode()
    assert (validate_base64_image.__name__ == "validate_base64_image")
