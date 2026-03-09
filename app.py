"""
Phase 10 Multiplayer Server
Flask + Flask-SocketIO backend
"""

from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, leave_room, emit
import random
import string
from collections import defaultdict

app = Flask(__name__)
app.config['SECRET_KEY'] = 'phase10secretkey2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ─── Game Data ────────────────────────────────────────────────────────────────

COLORS = ["Red", "Blue", "Green", "Yellow"]
NUMBERS = list(range(1, 13))

PHASES = {
    1:  [("set", 3), ("set", 3)],
    2:  [("set", 3), ("run", 4)],
    3:  [("set", 4), ("run", 4)],
    4:  [("run", 7)],
    5:  [("run", 8)],
    6:  [("run", 9)],
    7:  [("set", 4), ("set", 4)],
    8:  [("color", 7)],
    9:  [("set", 5), ("set", 2)],
    10: [("set", 5), ("set", 3)],
}

PHASE_DESCRIPTIONS = {
    1: "2 Sets of 3",       2: "Set of 3 + Run of 4",
    3: "Set of 4 + Run of 4", 4: "Run of 7",
    5: "Run of 8",          6: "Run of 9",
    7: "2 Sets of 4",       8: "7 of One Color",
    9: "Set of 5 + Set of 2", 10: "Set of 5 + Set of 3",
}

# Active rooms: room_code -> game state dict
rooms = {}

# ─── Card Helpers ─────────────────────────────────────────────────────────────

def make_deck():
    import uuid
    deck = []
    for _ in range(2):
        for color in COLORS:
            for number in NUMBERS:
                deck.append({"type": "number", "color": color, "number": number, "id": str(uuid.uuid4())})
    for i in range(8):
        deck.append({"type": "Wild", "color": None, "number": None, "id": str(uuid.uuid4())})
    for i in range(4):
        deck.append({"type": "Skip", "color": None, "number": None, "id": str(uuid.uuid4())})
    random.shuffle(deck)
    return deck

def card_points(card):
    if card["type"] == "Wild": return 25
    if card["type"] == "Skip": return 15
    return 5 if card["number"] <= 9 else 10

# ─── Phase Validation ─────────────────────────────────────────────────────────

def is_set(cards, count):
    if len(cards) != count: return False
    numbers = [c["number"] for c in cards if c["type"] == "number"]
    wilds = sum(1 for c in cards if c["type"] == "Wild")
    if not numbers: return wilds == count
    target = numbers[0]
    mismatches = sum(1 for n in numbers if n != target)
    return mismatches <= wilds

def is_run(cards, count):
    if len(cards) != count: return False
    numbers = sorted(c["number"] for c in cards if c["type"] == "number")
    wilds = sum(1 for c in cards if c["type"] == "Wild")
    if not numbers: return wilds >= count
    for start in range(1, 13):
        end = start + count - 1
        if end > 12: break
        run_range = list(range(start, end + 1))
        gaps = sum(1 for n in run_range if n not in numbers)
        extras = sum(1 for n in numbers if n not in run_range)
        if extras == 0 and gaps <= wilds: return True
    return False

def is_color(cards, count):
    if len(cards) != count: return False
    colors = [c["color"] for c in cards if c["type"] == "number"]
    wilds = sum(1 for c in cards if c["type"] == "Wild")
    if not colors: return wilds == count
    target = colors[0]
    mismatches = sum(1 for col in colors if col != target)
    return mismatches <= wilds

def validate_phase(phase_num, groups):
    requirements = PHASES[phase_num]
    if len(groups) != len(requirements):
        return False, "Wrong number of groups"
    for i, (req_type, req_count) in enumerate(requirements):
        grp = groups[i]
        if req_type == "set" and not is_set(grp, req_count):
            return False, f"Group {i+1} must be a set of {req_count}"
        if req_type == "run" and not is_run(grp, req_count):
            return False, f"Group {i+1} must be a run of {req_count}"
        if req_type == "color" and not is_color(grp, req_count):
            return False, f"Group {i+1} must be {req_count} cards of one color"
    return True, "Valid"

def group_type(grp):
    numbers = [c["number"] for c in grp if c["type"] == "number"]
    colors = [c["color"] for c in grp if c["type"] == "number"]
    if not numbers: return "unknown"
    if len(set(numbers)) == 1: return "set"
    if colors and len(set(colors)) == 1: return "color"
    return "run"

def can_add_to_group(card, grp):
    if card["type"] == "Wild": return True
    gtype = group_type(grp)
    numbers = sorted(c["number"] for c in grp if c["type"] == "number")
    colors = [c["color"] for c in grp if c["type"] == "number"]
    if gtype == "set":
        return not numbers or card["number"] == numbers[0]
    if gtype == "color":
        return not colors or card["color"] == colors[0]
    if gtype == "run":
        if not numbers: return True
        wilds = sum(1 for c in grp if c["type"] == "Wild")
        # Find the effective range the run currently occupies
        # Try all possible starting points and see if the group fits
        # then check if our card extends it at either end
        grp_size = len(grp)
        for start in range(1, 13):
            end = start + grp_size - 1
            if end > 12: break
            run_range = list(range(start, end + 1))
            gaps = sum(1 for n in run_range if n not in numbers)
            extras = sum(1 for n in numbers if n not in run_range)
            if extras == 0 and gaps <= wilds:
                # This is a valid placement — check if card extends it
                if start > 1 and card["number"] == start - 1: return True
                if end < 12 and card["number"] == end + 1: return True
        return False
    return False

# ─── Room Management ──────────────────────────────────────────────────────────

def make_room_code():
    return ''.join(random.choices(string.ascii_uppercase, k=4))

def init_round(room):
    state = rooms[room]
    deck = make_deck()
    hands = {}
    for pid in state["players"]:
        hands[pid] = []
        for _ in range(10):
            hands[pid].append(deck.pop())
    discard = [deck.pop()]
    state["deck"] = deck
    state["hands"] = hands
    state["discard"] = discard
    state["laid_down"] = {}   # pid -> list of groups
    state["skipped"] = set()
    state["round_over"] = False
    state["drawn"] = False    # has current player drawn yet

def broadcast_state(room):
    state = rooms[room]
    # Build per-player state (hide other hands)
    for pid in state["players"]:
        sid = state["player_sids"].get(pid)
        if not sid: continue
        player_state = {
            "room": room,
            "phase": "lobby" if state["phase"] == "lobby" else "game",
            "players": state["players"],
            "player_names": state["player_names"],
            "phases": state["phases"],
            "scores": state["scores"],
            "current_turn": state.get("current_turn"),
            "round_num": state.get("round_num", 0),
            "discard_top": state["discard"][-1] if state.get("discard") else None,
            "hand": state["hands"].get(pid, []) if state.get("hands") else [],
            "hand_counts": {p: len(state["hands"].get(p, [])) for p in state["players"]} if state.get("hands") else {},
            "laid_down": {p: grps for p, grps in state.get("laid_down", {}).items()},
            "drawn": state.get("drawn", False),
            "skipped": list(state.get("skipped", set())),
            "round_over": state.get("round_over", False),
            "game_over": state.get("game_over", False),
            "winner": state.get("winner"),
            "host": state["host"],
            "message": state.get("message", ""),
            "phase_descriptions": PHASE_DESCRIPTIONS,
            "my_id": pid,
        }
        socketio.emit("state_update", player_state, room=sid)

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ─── Socket Events ────────────────────────────────────────────────────────────

@socketio.on("create_room")
def on_create_room(data):
    name = data.get("name", "Player").strip()[:16]
    code = make_room_code()
    while code in rooms:
        code = make_room_code()
    pid = request.sid
    rooms[code] = {
        "phase": "lobby",
        "players": [pid],
        "player_names": {pid: name},
        "player_sids": {pid: pid},
        "host": pid,
        "phases": {pid: 1},
        "scores": {pid: 0},
        "deck": [], "hands": {}, "discard": [],
        "laid_down": {}, "skipped": set(),
        "round_num": 0, "round_over": False,
        "drawn": False, "message": "",
        "game_over": False, "winner": None,
        "current_turn": None,
    }
    join_room(code)
    emit("room_created", {"code": code, "pid": pid})
    broadcast_state(code)

@socketio.on("join_room_req")
def on_join_room(data):
    code = data.get("code", "").upper().strip()
    name = data.get("name", "Player").strip()[:16]
    pid = request.sid
    if code not in rooms:
        emit("error", {"msg": "Room not found."})
        return
    state = rooms[code]
    if state["phase"] != "lobby":
        emit("error", {"msg": "Game already started."})
        return
    if len(state["players"]) >= 6:
        emit("error", {"msg": "Room is full (max 6)."})
        return
    state["players"].append(pid)
    state["player_names"][pid] = name
    state["player_sids"][pid] = pid
    state["phases"][pid] = 1
    state["scores"][pid] = 0
    join_room(code)
    emit("room_joined", {"code": code, "pid": pid})
    broadcast_state(code)

@socketio.on("start_game")
def on_start_game(data):
    code = data.get("code")
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["host"] != pid: return
    if len(state["players"]) < 2:
        emit("error", {"msg": "Need at least 2 players."})
        return
    state["phase"] = "game"
    state["round_num"] = 1
    order = list(state["players"])
    random.shuffle(order)
    state["turn_order"] = order
    state["current_turn"] = order[0]
    init_round(code)
    state["message"] = f"Round 1 started! {state['player_names'][order[0]]} goes first."
    broadcast_state(code)

@socketio.on("draw_card")
def on_draw(data):
    code = data.get("code")
    source = data.get("source")  # "deck" or "discard"
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["current_turn"] != pid: return
    if state["drawn"]: return
    if pid in state["skipped"]:
        state["skipped"].discard(pid)
        _advance_turn(code)
        return
    if source == "deck" and state["deck"]:
        card = state["deck"].pop()
        state["hands"][pid].append(card)
        state["drawn"] = True
        state["message"] = f"{state['player_names'][pid]} drew from deck."
    elif source == "discard" and state["discard"]:
        card = state["discard"].pop()
        state["hands"][pid].append(card)
        state["drawn"] = True
        state["message"] = f"{state['player_names'][pid]} took from discard."
    # Reshuffle if deck empty
    if not state["deck"] and len(state["discard"]) > 1:
        top = state["discard"].pop()
        random.shuffle(state["discard"])
        state["deck"] = state["discard"]
        state["discard"] = [top]
    broadcast_state(code)

@socketio.on("lay_phase")
def on_lay_phase(data):
    code = data.get("code")
    groups_ids = data.get("groups")  # list of lists of card ids
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["current_turn"] != pid: return
    if not state["drawn"]: return
    if pid in state["laid_down"]: return
    phase_num = state["phases"][pid]
    if phase_num > 10: return
    # Resolve card ids to card objects
    hand = state["hands"][pid]
    hand_map = {c["id"]: c for c in hand}
    groups = []
    used_ids = set()
    for gids in groups_ids:
        grp = []
        for cid in gids:
            if cid not in hand_map or cid in used_ids:
                emit("error", {"msg": "Invalid card selection."})
                return
            grp.append(hand_map[cid])
            used_ids.add(cid)
        groups.append(grp)
    valid, msg = validate_phase(phase_num, groups)
    if not valid:
        emit("error", {"msg": msg})
        return
    # Remove used cards from hand
    state["hands"][pid] = [c for c in hand if c["id"] not in used_ids]
    state["laid_down"][pid] = groups
    state["message"] = f"🎉 {state['player_names'][pid]} laid down Phase {phase_num}!"
    # Check if all players have laid down their phase → end round immediately
    all_laid = all(p in state["laid_down"] for p in state["players"])
    if all_laid:
        _end_round(code, pid)
        return
    broadcast_state(code)

@socketio.on("hit_on_phase")
def on_hit(data):
    code = data.get("code")
    card_id = data.get("card_id")
    target_pid = data.get("target_pid")
    group_idx = data.get("group_idx")
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["current_turn"] != pid: return
    if not state["drawn"]: return
    if pid not in state["laid_down"]: return
    if target_pid not in state["laid_down"]: return
    hand = state["hands"][pid]
    card = next((c for c in hand if c["id"] == card_id), None)
    if not card:
        emit("error", {"msg": "Card not in hand."})
        return
    grp = state["laid_down"][target_pid][group_idx]
    if not can_add_to_group(card, grp):
        emit("error", {"msg": "Can't add that card to that group."})
        return
    state["laid_down"][target_pid][group_idx].append(card)
    state["hands"][pid] = [c for c in hand if c["id"] != card_id]
    state["message"] = f"{state['player_names'][pid]} hit on {state['player_names'][target_pid]}'s phase!"
    # Check if hand empty
    if not state["hands"][pid]:
        _end_round(code, pid)
        return
    broadcast_state(code)

@socketio.on("play_skip")
def on_skip(data):
    code = data.get("code")
    card_id = data.get("card_id")
    target_pid = data.get("target_pid")
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["current_turn"] != pid: return
    if not state["drawn"]: return
    hand = state["hands"][pid]
    card = next((c for c in hand if c["id"] == card_id), None)
    if not card or card["type"] != "Skip": return
    state["hands"][pid] = [c for c in hand if c["id"] != card_id]
    state["skipped"].add(target_pid)
    state["discard"].append(card)
    state["message"] = f"⛔ {state['player_names'][target_pid]} will be skipped!"
    # Playing a skip counts as the discard — end the turn
    if not state["hands"][pid]:
        _end_round(code, pid)
        return
    _advance_turn(code)

@socketio.on("discard_card")
def on_discard(data):
    code = data.get("code")
    card_id = data.get("card_id")
    pid = request.sid
    if code not in rooms: return
    state = rooms[code]
    if state["current_turn"] != pid: return
    if not state["drawn"]: return
    hand = state["hands"][pid]
    card = next((c for c in hand if c["id"] == card_id), None)
    if not card:
        emit("error", {"msg": "Card not in hand."})
        return
    state["hands"][pid] = [c for c in hand if c["id"] != card_id]
    state["discard"].append(card)
    state["message"] = f"{state['player_names'][pid]} discarded."
    # Check if hand empty → round over
    if not state["hands"][pid]:
        _end_round(code, pid)
        return
    _advance_turn(code)

def _advance_turn(code):
    state = rooms[code]
    order = state["turn_order"]
    cur = state["current_turn"]
    idx = order.index(cur)
    next_idx = (idx + 1) % len(order)
    state["current_turn"] = order[next_idx]
    state["drawn"] = False
    next_name = state["player_names"][state["current_turn"]]
    if state["current_turn"] in state["skipped"]:
        state["message"] = f"⛔ {next_name} is skipped!"
        state["skipped"].discard(state["current_turn"])
        _advance_turn(code)
        return
    state["message"] = f"It's {next_name}'s turn."
    broadcast_state(code)

def _end_round(code, winner_pid):
    state = rooms[code]
    state["round_over"] = True
    # Score hands
    for pid in state["players"]:
        pts = sum(card_points(c) for c in state["hands"].get(pid, []))
        state["scores"][pid] += pts
    # Advance phases for those who laid down
    for pid in state["players"]:
        if pid in state["laid_down"]:
            state["phases"][pid] = min(state["phases"][pid] + 1, 11)
    # Check game over
    finishers = [p for p in state["players"] if state["phases"][p] > 10]
    if finishers:
        champ = min(finishers, key=lambda p: state["scores"][p])
        state["game_over"] = True
        state["winner"] = champ
        state["message"] = f"🏆 {state['player_names'][champ]} wins the game!"
    else:
        state["message"] = f"Round over! {state['player_names'][winner_pid]} went out. Starting next round..."
    broadcast_state(code)
    if not state["game_over"]:
        # Auto-start next round after a moment
        import threading
        def next_round():
            import time; time.sleep(4)
            if code not in rooms: return
            s = rooms[code]
            s["round_num"] += 1
            s["round_over"] = False
            s["drawn"] = False
            order = list(s["players"])
            random.shuffle(order)
            s["turn_order"] = order
            s["current_turn"] = order[0]
            init_round(code)
            s["message"] = f"Round {s['round_num']}! {s['player_names'][order[0]]} goes first."
            broadcast_state(code)
        threading.Thread(target=next_round, daemon=True).start()

@socketio.on("chat")
def on_chat(data):
    code = data.get("code")
    msg = data.get("msg", "").strip()[:200]
    pid = request.sid
    if code not in rooms or not msg: return
    state = rooms[code]
    if pid not in state["players"]: return
    name = state["player_names"].get(pid, "?")
    socketio.emit("chat_message", {"pid": pid, "name": name, "msg": msg}, room=code)

@socketio.on("disconnect")
def on_disconnect():
    pid = request.sid
    for code, state in list(rooms.items()):
        if pid in state["players"]:
            name = state["player_names"].get(pid, "Someone")
            state["players"].remove(pid)
            state["player_names"].pop(pid, None)
            state["player_sids"].pop(pid, None)
            if not state["players"]:
                del rooms[code]
            else:
                if state["host"] == pid:
                    state["host"] = state["players"][0]
                state["message"] = f"{name} left the game."
                broadcast_state(code)
            break

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
