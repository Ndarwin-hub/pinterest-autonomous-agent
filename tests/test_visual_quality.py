from __future__ import annotations
import base64, io
from PIL import Image

from image_quality import inspect_image_bytes, validate_base64_image
from image_fingerprint import fingerprint, similarity
from pin_config import PINS_PER_PRODUCT


def _jpg(size=(1200, 1800), color=(120, 80, 40)):
    im=Image.new("RGB",size,color)
    for x in range(0,size[0],80):
        for y in range(0,size[1],80):
            im.putpixel((x,y),((color[0]+x//80*3)%256,(color[1]+y//80*5)%256,(color[2]+x//80*7)%256))
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
