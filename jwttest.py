import hmac, hashlib, base64, json, urllib.request

def b64(d): return base64.urlsafe_b64encode(d).rstrip(b'=')
def jwt(payload, secret):
    h = b64(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    p = b64(json.dumps(payload).encode())
    s = b64(hmac.new(secret.encode(), h+b'.'+p, hashlib.sha256).digest())
    return (h+b'.'+p+b'.'+s).decode()

for label, sec in [("compiled-in default", "secret"),
                   ("configured secret", "<JWT_SECRET>")]:
    body = json.dumps({"c":"version","token":jwt({"c":"version"}, sec)}).encode()
    req = urllib.request.Request("http://localhost/coauthoring/CommandService.ashx",
                                 data=body, headers={"Content-Type":"application/json"})
    print(label, "->", urllib.request.urlopen(req).read().decode())
