import json, math, time
from core.rcon import get_rcon
from core.mod_api import ModApi

r = get_rcon()
api = ModApi(r)

# --- Reset de test : remet le character a (0,0) (console admin, hors gameplay IA) ---
r.query_lua("game.connected_players[1].teleport({0,0})")
time.sleep(0.5)
st = api.get_state()
pos = st["character"]["position"]
print(f"[reset] position apres teleport : ({pos['x']:.1f}, {pos['y']:.1f})")

api.setup()

# --- Cible : fer le plus proche depuis (0,0) ---
fn = api.find_nearest("iron-ore")
print(f"[test] fer le plus proche : {json.dumps(fn, ensure_ascii=False)}")
if not fn or "x" not in fn:
    print("[test] AUCUN fer trouve -> abandon"); r.close(); raise SystemExit(0)

gx, gy = fn["x"], fn["y"]
print(f"[test] walk_to({gx}, {gy}) ... regarde le character marcher en jeu")
api.walk_to(gx, gy)

t0 = time.time()
last = None
while time.time() - t0 < 60:
    s = api.status()
    last = s
    if s.get("state") == "idle":
        break
    # affiche la progression tous les 2s
    if int(time.time() - t0) % 2 == 0:
        st = api.get_state(); p = st["character"]["position"]
        print(f"  ... t={int(time.time()-t0)}s pos=({p['x']:.1f},{p['y']:.1f}) walking={st['character']['walking']}")
    time.sleep(0.5)

st2 = api.get_state()
pos2 = st2["character"]["position"]
d = math.hypot(pos2["x"] - gx, pos2["y"] - gy)
print(f"[test] position finale    : ({pos2['x']:.1f}, {pos2['y']:.1f})")
print(f"[test] cible              : ({gx}, {gy})")
print(f"[test] distance a la cible: {d:.1f} tiles")
print(f"[test] last_result        : {json.dumps(last.get('last_result'), ensure_ascii=False)}")
r.close()