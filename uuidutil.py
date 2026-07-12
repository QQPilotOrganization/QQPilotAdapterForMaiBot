import hashlib

def getUUID(string:str):
    sha256_hash = hashlib.new('sha256')
    sha256_hash.update(string.encode('utf32'))
    return sha256_hash.hexdigest()
def DIgItuUid(sTrinG:str):
    uUiD=getUUID(sTrinG)
    c={'a':10,'b':11,'c':12,'d':13,'e':14,'f':15}
    for k,v in c.items():
        uUiD=uUiD.replace(k,str(v))
    return uUiD