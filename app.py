"""
Phase 10 Multiplayer Server
Flask + Flask-SocketIO backend
"""

from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, emit
import random, string, uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'phase10secret2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

rooms = {}

# ── Card Data ─────────────────────────────────────────────────────────────────
P10_COLORS  = ["Red","Blue","Green","Yellow"]
P10_NUMBERS = list(range(1,13))
PHASES = {
    1:[("set",3),("set",3)],  2:[("set",3),("run",4)],
    3:[("set",4),("run",4)],  4:[("run",7)],
    5:[("run",8)],            6:[("run",9)],
    7:[("set",4),("set",4)],  8:[("color",7)],
    9:[("set",5),("set",2)],  10:[("set",5),("set",3)],
}
PHASE_DESCRIPTIONS = {
    1:"2 Sets of 3",         2:"Set of 3 + Run of 4",
    3:"Set of 4 + Run of 4", 4:"Run of 7",
    5:"Run of 8",            6:"Run of 9",
    7:"2 Sets of 4",         8:"7 of One Color",
    9:"Set of 5 + Set of 2", 10:"Set of 5 + Set of 3",
}

def make_deck():
    deck=[]
    for _ in range(2):
        for color in P10_COLORS:
            for number in P10_NUMBERS:
                deck.append({"type":"number","color":color,"number":number,"id":str(uuid.uuid4())})
    for _ in range(8): deck.append({"type":"Wild","color":None,"number":None,"id":str(uuid.uuid4())})
    for _ in range(4): deck.append({"type":"Skip","color":None,"number":None,"id":str(uuid.uuid4())})
    random.shuffle(deck)
    return deck

def card_points(card):
    if card["type"]=="Wild": return 25
    if card["type"]=="Skip": return 15
    return 5 if card["number"]<=9 else 10

# ── Phase Validation ──────────────────────────────────────────────────────────
def is_set(cards,count):
    if len(cards)!=count: return False
    numbers=[c["number"] for c in cards if c["type"]=="number"]
    wilds=sum(1 for c in cards if c["type"]=="Wild")
    if not numbers: return wilds==count
    return sum(1 for n in numbers if n!=numbers[0])<=wilds

def is_run(cards,count):
    if len(cards)!=count: return False
    numbers=sorted(c["number"] for c in cards if c["type"]=="number")
    wilds=sum(1 for c in cards if c["type"]=="Wild")
    if not numbers: return wilds>=count
    for start in range(1,13):
        end=start+count-1
        if end>12: break
        rng=list(range(start,end+1))
        if sum(1 for n in numbers if n not in rng)==0 and sum(1 for n in rng if n not in numbers)<=wilds: return True
    return False

def is_color(cards,count):
    if len(cards)!=count: return False
    colors=[c["color"] for c in cards if c["type"]=="number"]
    wilds=sum(1 for c in cards if c["type"]=="Wild")
    if not colors: return wilds==count
    return sum(1 for col in colors if col!=colors[0])<=wilds

def validate_phase(phase_num,groups):
    reqs=PHASES[phase_num]
    if len(groups)!=len(reqs): return False,"Wrong number of groups"
    for i,(rt,rc) in enumerate(reqs):
        g=groups[i]
        if rt=="set"   and not is_set(g,rc):   return False,f"Group {i+1} must be a set of {rc}"
        if rt=="run"   and not is_run(g,rc):   return False,f"Group {i+1} must be a run of {rc}"
        if rt=="color" and not is_color(g,rc): return False,f"Group {i+1} must be {rc} cards of one color"
    return True,"Valid"

def group_type(grp):
    numbers=[c["number"] for c in grp if c["type"]=="number"]
    colors=[c["color"] for c in grp if c["type"]=="number"]
    if not numbers: return "unknown"
    if len(set(numbers))==1: return "set"
    if colors and len(set(colors))==1: return "color"
    return "run"

def can_add(card,grp):
    if card["type"]=="Wild": return True
    gtype=group_type(grp)
    numbers=sorted(c["number"] for c in grp if c["type"]=="number")
    colors=[c["color"] for c in grp if c["type"]=="number"]
    wilds=sum(1 for c in grp if c["type"]=="Wild")
    if gtype=="set":   return not numbers or card["number"]==numbers[0]
    if gtype=="color": return not colors  or card["color"]==colors[0]
    if gtype=="run":
        if not numbers: return True
        for start in range(1,13):
            end=start+len(grp)-1
            if end>12: break
            rng=list(range(start,end+1))
            if sum(1 for n in numbers if n not in rng)==0 and sum(1 for n in rng if n not in numbers)<=wilds:
                if start>1 and card["number"]==start-1: return True
                if end<12  and card["number"]==end+1:   return True
    return False

# ── Room Helpers ──────────────────────────────────────────────────────────────
def make_room_code():
    code=''.join(random.choices(string.ascii_uppercase,k=4))
    while code in rooms: code=''.join(random.choices(string.ascii_uppercase,k=4))
    return code

def init_round(code):
    state=rooms[code]
    deck=make_deck()
    hands={}
    for pid in state["players"]:
        hands[pid]=[deck.pop() for _ in range(10)]
    state["deck"]=deck; state["hands"]=hands
    state["discard"]=[deck.pop()]
    state["laid_down"]={}; state["skipped"]=set()
    state["round_over"]=False; state["drawn"]=False

def broadcast(code):
    state=rooms[code]
    for pid in state["players"]:
        sid=state["player_sids"].get(pid)
        if not sid: continue
        socketio.emit("state_update",{
            "phase":      "lobby" if state["phase"]=="lobby" else "game",
            "players":    state["players"],
            "player_names":state["player_names"],
            "phases":     state["phases"],
            "scores":     state["scores"],
            "current_turn":state.get("current_turn"),
            "round_num":  state.get("round_num",0),
            "discard_top":state["discard"][-1] if state.get("discard") else None,
            "hand":       state["hands"].get(pid,[]) if state.get("hands") else [],
            "hand_counts":{p:len(state["hands"].get(p,[])) for p in state["players"]} if state.get("hands") else {},
            "laid_down":  state.get("laid_down",{}),
            "drawn":      state.get("drawn",False),
            "skipped":    list(state.get("skipped",set())),
            "round_over": state.get("round_over",False),
            "game_over":  state.get("game_over",False),
            "winner":     state.get("winner"),
            "host":       state["host"],
            "message":    state.get("message",""),
            "phase_descriptions":PHASE_DESCRIPTIONS,
            "my_id":      pid,
            "room":       code,
            "lobby_phase":"setup" if state.get("lobby_phase")=="setup" else "waiting",
            "custom_phases":state.get("custom_phases",{}),
        },room=sid)

def advance_turn(code):
    state=rooms[code]
    order=state["turn_order"]
    idx=order.index(state["current_turn"])
    nxt=order[(idx+1)%len(order)]
    state["current_turn"]=nxt; state["drawn"]=False
    name=state["player_names"][nxt]
    if nxt in state["skipped"]:
        state["skipped"].discard(nxt)
        state["message"]=f"⛔ {name} is skipped!"
        advance_turn(code); return
    state["message"]=f"It's {name}'s turn."
    broadcast(code)

def end_round(code,winner_pid):
    state=rooms[code]; state["round_over"]=True
    for pid in state["players"]:
        state["scores"][pid]+=sum(card_points(c) for c in state["hands"].get(pid,[]))
    for pid in state["players"]:
        if pid in state["laid_down"]:
            state["phases"][pid]=min(state["phases"][pid]+1,11)
    finishers=[p for p in state["players"] if state["phases"][p]>10]
    if finishers:
        champ=winner_pid if winner_pid in finishers else min(finishers,key=lambda p:state["scores"][p])
        state["game_over"]=True; state["winner"]=champ
        state["message"]=f"🏆 {state['player_names'][champ]} completed Phase 10 and wins!"
    else:
        state["message"]="Round over! Starting next round..."
    broadcast(code)
    if not state["game_over"]:
        import threading,time
        def next_round():
            time.sleep(4)
            if code not in rooms: return
            s=rooms[code]; s["round_num"]+=1; s["round_over"]=False; s["drawn"]=False
            order=list(s["players"]); random.shuffle(order)
            s["turn_order"]=order; s["current_turn"]=order[0]
            init_round(code)
            s["message"]=f"Round {s['round_num']}! {s['player_names'][order[0]]} goes first."
            broadcast(code)
        threading.Thread(target=next_round,daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ── Socket Events ─────────────────────────────────────────────────────────────
@socketio.on("create_room")
def on_create_room(data):
    name=data.get("name","Player").strip()[:16]; pid=request.sid
    code=make_room_code()
    rooms[code]={
        "phase":"lobby","lobby_phase":"waiting",
        "players":[pid],"player_names":{pid:name},
        "player_sids":{pid:pid},"host":pid,
        "phases":{pid:1},"scores":{pid:0},
        "custom_phases":{},
        "deck":[],"hands":{},"discard":[],
        "laid_down":{},"skipped":set(),
        "round_num":0,"round_over":False,
        "drawn":False,"message":"",
        "game_over":False,"winner":None,"current_turn":None,
    }
    join_room(code)
    emit("room_created",{"code":code,"pid":pid})
    broadcast(code)

@socketio.on("join_room_req")
def on_join_room(data):
    code=data.get("code","").upper().strip()
    name=data.get("name","Player").strip()[:16]; pid=request.sid
    if code not in rooms: emit("error",{"msg":"Room not found."}); return
    state=rooms[code]
    if state["phase"]!="lobby": emit("error",{"msg":"Game already started."}); return
    if len(state["players"])>=8: emit("error",{"msg":"Room is full (max 8)."}); return
    state["players"].append(pid); state["player_names"][pid]=name
    state["player_sids"][pid]=pid; state["phases"][pid]=1; state["scores"][pid]=0
    join_room(code)
    emit("room_joined",{"code":code,"pid":pid})
    broadcast(code)

@socketio.on("set_phase")
def on_set_phase(data):
    """Host sets a player's starting phase."""
    code=data.get("code"); target=data.get("target_pid"); phase=data.get("phase",1); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["host"]!=pid: return
    phase=max(1,min(10,int(phase)))
    state["phases"][target]=phase
    state["custom_phases"][target]=phase
    broadcast(code)

@socketio.on("enter_setup")
def on_enter_setup(data):
    """Host enters the phase setup screen."""
    code=data.get("code"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["host"]!=pid: return
    state["lobby_phase"]="setup"
    broadcast(code)

@socketio.on("exit_setup")
def on_exit_setup(data):
    """Host goes back to normal waiting."""
    code=data.get("code"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["host"]!=pid: return
    state["lobby_phase"]="waiting"
    broadcast(code)

@socketio.on("start_game")
def on_start_game(data):
    code=data.get("code"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["host"]!=pid: return
    if len(state["players"])<2: emit("error",{"msg":"Need at least 2 players."}); return
    state["phase"]="game"; state["round_num"]=1; state["lobby_phase"]="waiting"
    order=list(state["players"]); random.shuffle(order)
    state["turn_order"]=order; state["current_turn"]=order[0]
    init_round(code)
    state["message"]=f"Round 1! {state['player_names'][order[0]]} goes first."
    broadcast(code)

@socketio.on("draw_card")
def on_draw(data):
    code=data.get("code"); source=data.get("source"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["current_turn"]!=pid or state["drawn"]: return
    if source=="deck" and state["deck"]:
        state["hands"][pid].append(state["deck"].pop()); state["drawn"]=True
        state["message"]=f"{state['player_names'][pid]} drew from deck."
    elif source=="discard" and state["discard"]:
        state["hands"][pid].append(state["discard"].pop()); state["drawn"]=True
        state["message"]=f"{state['player_names'][pid]} took from discard."
    if not state["deck"] and len(state["discard"])>1:
        top=state["discard"].pop(); random.shuffle(state["discard"])
        state["deck"]=state["discard"]; state["discard"]=[top]
    broadcast(code)

@socketio.on("lay_phase")
def on_lay_phase(data):
    code=data.get("code"); groups_ids=data.get("groups"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["current_turn"]!=pid or not state["drawn"] or pid in state["laid_down"]: return
    phase_num=state["phases"][pid]
    if phase_num>10: return
    hand=state["hands"][pid]; hand_map={c["id"]:c for c in hand}
    groups=[]; used=set()
    for gids in groups_ids:
        grp=[]
        for cid in gids:
            if cid not in hand_map or cid in used: emit("error",{"msg":"Invalid card."}); return
            grp.append(hand_map[cid]); used.add(cid)
        groups.append(grp)
    valid,msg=validate_phase(phase_num,groups)
    if not valid: emit("error",{"msg":msg}); return
    state["hands"][pid]=[c for c in hand if c["id"] not in used]
    state["laid_down"][pid]=groups
    state["message"]=f"🎉 {state['player_names'][pid]} laid down Phase {phase_num}!"
    if phase_num==10: end_round(code,pid); return
    if all(p in state["laid_down"] for p in state["players"]): end_round(code,pid); return
    broadcast(code)

@socketio.on("hit_on_phase")
def on_hit(data):
    code=data.get("code"); card_id=data.get("card_id"); target=data.get("target_pid"); gi=data.get("group_idx"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["current_turn"]!=pid or not state["drawn"] or pid not in state["laid_down"] or target not in state["laid_down"]: return
    hand=state["hands"][pid]; card=next((c for c in hand if c["id"]==card_id),None)
    if not card: emit("error",{"msg":"Card not in hand."}); return
    if not can_add(card,state["laid_down"][target][gi]): emit("error",{"msg":"Can't add that card."}); return
    state["laid_down"][target][gi].append(card)
    state["hands"][pid]=[c for c in hand if c["id"]!=card_id]
    state["message"]=f"{state['player_names'][pid]} hit on {state['player_names'][target]}'s phase!"
    if not state["hands"][pid]: end_round(code,pid); return
    broadcast(code)

@socketio.on("play_skip")
def on_skip(data):
    code=data.get("code"); card_id=data.get("card_id"); target=data.get("target_pid"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["current_turn"]!=pid or not state["drawn"]: return
    hand=state["hands"][pid]; card=next((c for c in hand if c["id"]==card_id),None)
    if not card or card["type"]!="Skip": return
    state["hands"][pid]=[c for c in hand if c["id"]!=card_id]
    state["skipped"].add(target); state["discard"].append(card)
    state["message"]=f"⛔ {state['player_names'][target]} will be skipped!"
    if not state["hands"][pid]: end_round(code,pid); return
    advance_turn(code)

@socketio.on("discard_card")
def on_discard(data):
    code=data.get("code"); card_id=data.get("card_id"); pid=request.sid
    if code not in rooms: return
    state=rooms[code]
    if state["current_turn"]!=pid or not state["drawn"]: return
    hand=state["hands"][pid]; card=next((c for c in hand if c["id"]==card_id),None)
    if not card: emit("error",{"msg":"Card not in hand."}); return
    state["hands"][pid]=[c for c in hand if c["id"]!=card_id]
    state["discard"].append(card); state["message"]=f"{state['player_names'][pid]} discarded."
    if not state["hands"][pid]: end_round(code,pid); return
    advance_turn(code)

@socketio.on("chat")
def on_chat(data):
    code=data.get("code"); msg=data.get("msg","").strip()[:200]; pid=request.sid
    if code not in rooms or not msg: return
    state=rooms[code]
    if pid not in state["players"]: return
    socketio.emit("chat_message",{"pid":pid,"name":state["player_names"].get(pid,"?"),"msg":msg},room=code)

@socketio.on("disconnect")
def on_disconnect():
    pid=request.sid
    for code,state in list(rooms.items()):
        if pid in state["players"]:
            name=state["player_names"].get(pid,"Someone")
            state["players"].remove(pid); state["player_names"].pop(pid,None); state["player_sids"].pop(pid,None)
            if not state["players"]: del rooms[code]
            else:
                if state["host"]==pid: state["host"]=state["players"][0]
                state["message"]=f"{name} left the game."
                broadcast(code)
            break

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
