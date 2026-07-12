import base64
from PIL import Image
import io


def base64ToImage(b:str):
    return Image.open(io.BytesIO(base64.b64decode(b)))

def isEmoji(img:Image.Image):
    return (img.width==img.height and img.width<1024) or (img.width<128 and img.height<128)