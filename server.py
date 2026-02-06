# -*- coding: utf-8 -*-
"""
双人联网答题对战 - 服务端
双方收到相同题目，1分钟内作答正确得1分，得分高者获胜。
"""
import random
import eventlet
eventlet.monkey_patch()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = "math-battle-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# 游戏状态
MAX_ROUNDS = 50  # 每个玩家最多答题数（足够大，主要限制是时间）
TIME_LIMIT = 60  # 整局总时间（秒）

state = {
    "player1_sid": None,
    "player2_sid": None,
    "player1_score": 0,
    "player2_score": 0,
    # 统一题库（保证题目顺序相同），每个元素: {"text": str, "answer": int}
    "questions": [],
    # 每个玩家当前做到第几题（索引），互相独立
    "p1_index": 0,
    "p2_index": 0,
    # 每个玩家当前题目是否仍在答题中
    "p1_open": False,
    "p2_open": False,
    "game_timer": None,  # 整局 1 分钟计时
    "game_over": False,
}


def generate_question():
    """随机生成一道题：4位数加减法 或 2位数乘法。返回 (题目文本, 正确答案)"""
    choice = random.choice(["four_digit", "two_digit"])
    if choice == "four_digit":
        op = random.choice(["+", "-"])
        if op == "+":
            a, b = random.randint(1000, 9999), random.randint(1000, 9999)
            return f"{a} + {b} = ?", a + b
        else:
            a, b = random.randint(1000, 9999), random.randint(1000, 9999)
            if a < b:
                a, b = b, a
            return f"{a} - {b} = ?", a - b
    else:
        a, b = random.randint(10, 99), random.randint(10, 99)
        return f"{a} × {b} = ?", a * b


def get_both_sids():
    return state["player1_sid"], state["player2_sid"]


def emit_to_both(event, data):
    p1, p2 = get_both_sids()
    if p1:
        socketio.emit(event, data, room=p1)
    if p2:
        socketio.emit(event, data, room=p2)


def get_player_by_sid(sid):
    """根据 sid 返回玩家编号 1/2 或 None"""
    if sid == state["player1_sid"]:
        return 1
    if sid == state["player2_sid"]:
        return 2
    return None


def ensure_question(index):
    """确保题库中存在给定 index 的题目，不足则生成追加"""
    while len(state["questions"]) <= index and index < MAX_ROUNDS:
        q_text, ans = generate_question()
        state["questions"].append({"text": q_text, "answer": ans})


def send_question_to_player(player):
    """仅给指定玩家下发下一题，双方题目来源相同但进度独立"""
    if state["game_over"]:
        return

    if player == 1:
        idx_key, open_key, sid = "p1_index", "p1_open", state["player1_sid"]
    else:
        idx_key, open_key, sid = "p2_index", "p2_open", state["player2_sid"]

    if not sid:
        return

    idx = state[idx_key]
    if idx >= MAX_ROUNDS:
        return  # 达到个人最大题量，不再出题

    ensure_question(idx)
    q = state["questions"][idx]
    state[open_key] = True

    socketio.emit(
        "question",
        {
            "question": q["text"],
            "round": idx + 1,
            "total_rounds": MAX_ROUNDS,
        },
        room=sid,
    )


def end_game():
    """结束整局游戏（到时或做完所有题）"""
    if state["game_over"]:
        return
    state["game_over"] = True

    if state["game_timer"]:
        try:
            state["game_timer"].cancel()
        except Exception:
            pass
        state["game_timer"] = None

    winner = None
    if state["player1_score"] > state["player2_score"]:
        winner = 1
    elif state["player2_score"] > state["player1_score"]:
        winner = 2

    emit_to_both("game_over", {
        "scores": [state["player1_score"], state["player2_score"]],
        "winner": winner,
    })


def next_round():
    if state["game_over"]:
        return
    q_text, answer = generate_question()
    state["current_question_text"] = q_text
    state["current_answer"] = answer
    state["answers"] = {}
    state["question_open"] = True

    emit_to_both("question", {
        "question": q_text,
        "round": state["round"] + 1,
        "total_rounds": MAX_ROUNDS,
    })


def start_game():
    state["player1_score"] = 0
    state["player2_score"] = 0
    state["questions"] = []
    state["p1_index"] = 0
    state["p2_index"] = 0
    state["p1_open"] = False
    state["p2_open"] = False
    state["game_over"] = False

    # 启动整局 1 分钟计时，时间到直接结束游戏
    if state["game_timer"]:
        try:
            state["game_timer"].cancel()
        except Exception:
            pass
    state["game_timer"] = eventlet.spawn_after(TIME_LIMIT, end_game)

    emit_to_both("game_start", {"max_rounds": MAX_ROUNDS, "time_limit": TIME_LIMIT})
    # 双方各自从第 1 题开始，进度独立
    send_question_to_player(1)
    send_question_to_player(2)


@socketio.on("connect")
def on_connect():
    pass


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if state["player1_sid"] == sid:
        state["player1_sid"] = None
    if state["player2_sid"] == sid:
        state["player2_sid"] = None


@socketio.on("join")
def on_join(data):
    player = data.get("player")
    if player == 1:
        state["player1_sid"] = request.sid
    elif player == 2:
        state["player2_sid"] = request.sid
    emit("joined", {"player": player})

    if state["player1_sid"] and state["player2_sid"]:
        start_game()


@socketio.on("answer")
def on_answer(data):
    if state["game_over"]:
        return

    player = get_player_by_sid(request.sid)
    if player is None:
        return

    # 取当前玩家的题目索引与状态
    if player == 1:
        idx_key, open_key = "p1_index", "p1_open"
    else:
        idx_key, open_key = "p2_index", "p2_open"

    if not state[open_key]:
        return  # 当前题目已结算或未出题，忽略

    idx = state[idx_key]
    if idx >= len(state["questions"]):
        return

    q = state["questions"][idx]
    correct_answer = q["answer"]
    raw_value = data.get("value")

    try:
        ok = int(raw_value) == correct_answer
    except (TypeError, ValueError):
        ok = False

    # 结算该玩家本题
    if player == 1:
        if ok:
            state["player1_score"] += 1
        state["p1_open"] = False
    else:
        if ok:
            state["player2_score"] += 1
        state["p2_open"] = False

    # 当前玩家前进到下一题
    state[idx_key] += 1

    # 向双方广播本次答题结果（标明是哪位玩家提交）
    p1_ok = ok if player == 1 else False
    p2_ok = ok if player == 2 else False

    emit_to_both(
        "round_result",
        {
            "scores": [state["player1_score"], state["player2_score"]],
            "correct_answer": correct_answer,
            "correct": [p1_ok, p2_ok],
            "round": idx + 1,
            "player": player,
        },
    )

    # 如果时间未到且题量未达上限，为该玩家下发下一题
    if not state["game_over"] and state[idx_key] < MAX_ROUNDS:
        send_question_to_player(player)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
