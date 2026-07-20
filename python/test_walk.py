import json, time
from core.rcon import get_rcon
from core.mod_api import ModApi

r = get_rcon()
api = ModApi(r)

api.setup()
st = api.get_state()
pos = st["character"]["position"]
print(f"[test] position initiale : ({pos['x']:.1f}, {pos['y']:.1f})")

fn = api.find_nearest("iron-ore")
print(f"[test] fer le plus proche : {json.dumps(fn, ensure_ascii=False)}")
if not fn or "x" not in fn:
    print("[test] AUCUN fer trouve -> abandon")
    r.close(); raise SystemExit(0)

gx, gy = fn["x"], fn["y"]
print(f"[test] walk_to({gx}, {gy}) ... (regarde le character en jeu)")
api.walk_to(gx, gy)

t0 = time.time()
last = None
while time.time() - t0 < 45:
    s = api.status()
    if s.get("state") == "idle":
        last = s
        break
    time.sleep(0.5)
else:
    last = api.status()
    print("[test] TIMEOUT (45s) - status:", json.dumps(last, ensure_ascii=False))

st2 = api.get_state()
pos2 = st2["character"]["position"]
print(f"[test] position finale    : ({pos2['x']:.1f}, {pos2['y']:.1f})")
print(f"[test] cible              : ({gx}, {gy})")
import math
d = math.hypot(pos2["x"] - gx, pos2["y"] - gy)
print(f"[test] distance a la cible: {d:.1f} tiles")
print(f"[test] last_result        : {json.dumps(last.get('last_result'), ensure_ascii=False)}")
r.close()