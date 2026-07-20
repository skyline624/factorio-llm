import json, time
from core.rcon import get_rcon
from core.mod_api import ModApi

r = get_rcon()
api = ModApi(r)

# Reset a (0,0)
r.query_lua("game.connected_players[1].teleport({0,0})")
time.sleep(0.5)
api.setup()
st = api.get_state()
print(f"[test] position depart : {st['character']['position']}")

# Cible fixe (le candidat fer d'origine)
gx, gy = 72.5, 46.5
print(f"[test] walk_to({gx}, {gy}) ...")
api.walk_to(gx, gy)

# Laisse tourner ~15s pour capturer les logs [DBG] (1 log / 30 ticks = 0.5s)
t0 = time.time()
while time.time() - t0 < 15:
    s = api.status()
    if s.get("state") == "idle":
        print(f"[test] idle a t={int(time.time()-t0)}s : {json.dumps(s.get('last_result'), ensure_ascii=False)}")
        break
    time.sleep(0.5)
else:
    print(f"[test] toujours busy apres 15s : {json.dumps(api.status(), ensure_ascii=False)}")

st2 = api.get_state()
print(f"[test] position finale : {st2['character']['position']} walking={st2['character']['walking']}")
r.close()