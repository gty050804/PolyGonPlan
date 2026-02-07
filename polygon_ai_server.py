# -*- coding: utf-8 -*-
"""
多边形连线对战游戏 - 人机对战版（单人 + 虚拟对手 AI）

规则与 `polygon_server.py` 基本一致：
- 画布上随机分布：
  - 16 个白点、8 个蓝点、2 个绿点、4 个红点
  - 15 个黑色空心小圆环（只允许在圆环圆心之间连线）
- 每局 5 分钟，总共 5 局 3 胜：
  - 玩家需要在 5 分钟内，用线段依次连接 15 个圆环圆心，形成不自交的 15 边形回路；
  - 验证通过后，统计 15 边形内部的点数得分：
    - 白点：+1 分，蓝点：+2 分，绿点：+5 分，红点：-4 分
  - 若一方未在时限内形成合法 15 边形而对方完成，则对方直接胜利；
  - 若双方都完成，则比较得分，高者胜；
  - 若本局得分相同，则以「提交用时更短者」获胜。

区别：
- 只需要一个真实玩家，另一方由服务器内置 AI 扮演（昵称固定为 "AI"）。
- AI 策略：根据圆环的几何分布构造一个大致「凸状」的 15 边形，目标是包住尽可能多的点，以获取较高得分。
"""

import math
import random
import time
import eventlet

eventlet.monkey_patch()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = "polygon-ai-battle-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# 画布设置
BOARD_W = 800
BOARD_H = 800
MARGIN = 40

# 数量设置（80 个原本得 1 分的点，这里用白点表示）
NUM_WHITE = 16
NUM_BLUE = 8
NUM_GREEN = 2
NUM_RED = 4
NUM_RINGS = 15

TIME_LIMIT = 300   # 每局总时间 5 分钟（秒）
MATCH_WIN = 3      # 5 局 3 胜
NICKNAME_MAX_LEN = 20
MAX_AI_SEARCH_TIME = 15.0  # AI 搜索时间上限（秒），可适当调大（必须 < TIME_LIMIT）

state = {
    "human_sid": None,
    "nickname_human": "玩家",
    "nickname_ai": "AI",
    "ai_difficulty": "normal",  # easy / normal / hard
    # 题板
    "dots": [],   # 每个元素: {"x":float,"y":float,"color":str}
    "rings": [],  # 每个元素: {"x":float,"y":float,"id":int}
    # 当前局结果
    "edges_human": [],
    "edges_ai": [],
    "result_human": None,
    "result_ai": None,
    "game_timer": None,
    "game_over": False,
    # 5 局 3 胜
    "round_index": 1,
    "wins_human": 0,
    "wins_ai": 0,
    "ready": False,          # 玩家是否点击了“准备下一轮”
    "round_start_time": None,
    "both_submitted_at": None,  # 双方都提交（人+AI）时的时间，用于 3 秒后结束
}


def rand_point():
    """在画布中生成一个随机点（预留边距，避免靠边太近）"""
    return (
        random.uniform(MARGIN, BOARD_W - MARGIN),
        random.uniform(MARGIN, BOARD_H - MARGIN),
    )


def generate_board():
    """生成整盘棋的点和圆环（双方共用）"""
    dots = []
    for color, count in [
        ("white", NUM_WHITE),
        ("blue", NUM_BLUE),
        ("green", NUM_GREEN),
        ("red", NUM_RED),
    ]:
        for _ in range(count):
            x, y = rand_point()
            dots.append({"x": x, "y": y, "color": color})

    rings = []
    for i in range(NUM_RINGS):
        x, y = rand_point()
        rings.append({"x": x, "y": y, "id": i})

    state["dots"] = dots
    state["rings"] = rings


def emit_to_human(event, data):
    sid = state["human_sid"]
    if sid:
        socketio.emit(event, data, room=sid)


# ---------- 几何与判定函数 ----------

def segments_intersect(p1, p2, p3, p4):
    """判断线段 p1-p2 与 p3-p4 是否严格相交（不含端点重合）"""

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a, b, p):
        return (
            min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
        )

    c1 = cross(p1, p2, p3)
    c2 = cross(p1, p2, p4)
    c3 = cross(p3, p4, p1)
    c4 = cross(p3, p4, p2)

    # 一般相交
    if c1 * c2 < 0 and c3 * c4 < 0:
        return True

    # 共线接触不算“严格相交”
    if c1 == 0 and on_segment(p1, p2, p3):
        return False
    if c2 == 0 and on_segment(p1, p2, p4):
        return False
    if c3 == 0 and on_segment(p3, p4, p1):
        return False
    if c4 == 0 and on_segment(p3, p4, p2):
        return False

    return False


def polygon_is_simple_cycle(edges, num_vertices):
    """判断 edges 是否构成一个包含 num_vertices 个顶点的简单回路"""
    if len(edges) != num_vertices:
        return False

    # 度数必须都是 2
    deg = [0] * num_vertices
    adj = [[] for _ in range(num_vertices)]
    for a, b in edges:
        if a == b:
            return False
        if not (0 <= a < num_vertices and 0 <= b < num_vertices):
            return False
        deg[a] += 1
        deg[b] += 1
        adj[a].append(b)
        adj[b].append(a)
    if any(d != 2 for d in deg):
        return False

    # 连通性：从 0 号顶点出发，应该能走遍所有顶点
    visited = [False] * num_vertices
    stack = [0]
    visited[0] = True
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if not visited[v]:
                visited[v] = True
                stack.append(v)
    if not all(visited):
        return False

    # 检查线段是否相交（不允许不同边之间相交）
    rings = state["rings"]
    segments = [((rings[a]["x"], rings[a]["y"]), (rings[b]["x"], rings[b]["y"])) for a, b in edges]

    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            a1, a2 = edges[i]
            b1, b2 = edges[j]
            # 共享端点的相邻边允许接触
            if a1 in (b1, b2) or a2 in (b1, b2):
                continue
            p1, p2 = segments[i]
            p3, p4 = segments[j]
            if segments_intersect(p1, p2, p3, p4):
                return False

    return True


def point_in_polygon(x, y, poly_pts):
    """射线法判断点是否在多边形内部（不含边界）"""
    inside = False
    n = len(poly_pts)
    for i in range(n):
        x1, y1 = poly_pts[i]
        x2, y2 = poly_pts[(i + 1) % n]
        # 只处理跨过水平射线的边
        if ((y1 > y) != (y2 > y)):
            xinters = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-9) + x1
            if xinters > x:
                inside = not inside
    return inside


def compute_score_and_counts(edges):
    """根据给定的边（圆环索引对）计算多边形得分及颜色计数"""
    if not polygon_is_simple_cycle(edges, NUM_RINGS):
        return None, None

    # 构造顶点顺序（从某顶点沿着邻接关系环游一次）
    adj = [[] for _ in range(NUM_RINGS)]
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)

    order = [0]
    prev = None
    cur = 0
    while True:
        neighbors = adj[cur]
        nxt = neighbors[0] if neighbors[0] != prev else neighbors[1]
        if nxt == 0:
            break
        order.append(nxt)
        prev, cur = cur, nxt

    rings = state["rings"]
    poly_pts = [(rings[i]["x"], rings[i]["y"]) for i in order]

    counts = {"white": 0, "blue": 0, "green": 0, "red": 0}
    score = 0
    for d in state["dots"]:
        if point_in_polygon(d["x"], d["y"], poly_pts):
            c = d["color"]
            if c in counts:
                counts[c] += 1
            if c == "white":
                score += 1
            elif c == "blue":
                score += 2
            elif c == "green":
                score += 5
            elif c == "red":
                score -= 4
    return score, counts


def _score_order(order):
    """给定顶点访问顺序 order，返回 (score, counts, edges)。非法多边形则返回 (None, None, None)。"""
    n = NUM_RINGS
    edges = [(order[i], order[(i + 1) % n]) for i in range(n)]
    score, counts = compute_score_and_counts(edges)
    if score is None:
        return None, None, None
    return score, counts, edges


def _initial_order():
    """按圆环相对质心的极角排序，作为初始解。"""
    rings = state["rings"]
    if len(rings) != NUM_RINGS:
        return list(range(NUM_RINGS))
    cx = sum(r["x"] for r in rings) / NUM_RINGS
    cy = sum(r["y"] for r in rings) / NUM_RINGS
    order = sorted(
        range(NUM_RINGS),
        key=lambda i: math.atan2(rings[i]["y"] - cy, rings[i]["x"] - cx),
    )
    return order


def _neighbor(order):
    """邻域生成：随机选择 swap 或 2-opt 操作，返回一个新排列。"""
    n = len(order)
    new_order = order[:]
    if n < 3:
        return new_order

    if random.random() < 0.5:
        # swap: 交换两个顶点位置
        i, j = random.sample(range(n), 2)
        new_order[i], new_order[j] = new_order[j], new_order[i]
    else:
        # 2-opt: 选一段区间翻转
        i, j = sorted(random.sample(range(n), 2))
        if j - i > 1:
            new_order[i : j + 1] = reversed(new_order[i : j + 1])
    return new_order


def calc_ai_result():
    """
    根据当前题板为 AI 计算一个多边形及得分（模拟退火 + 局部搜索）。

    - 解的表示：顶点访问顺序 order（一个 0..14 的排列）。
    - 初始解：极角排序（大致凸包形状）。
    - 邻域：swap / 2-opt。
    - 策略：在给定时间内（MAX_AI_SEARCH_TIME 秒）做模拟退火，
      接受更优解，按 exp(delta/T) 概率接受差解，持续更新全局最优。
    """
    rings = state["rings"]
    if len(rings) != NUM_RINGS:
        return {
            "finished": False,
            "valid": False,
            "score": 0,
            "counts": {"white": 0, "blue": 0, "green": 0, "red": 0},
            "edges": [],
            "submit_time": TIME_LIMIT,
        }

    start = time.time()
    difficulty = state.get("ai_difficulty", "normal")
    # 不同难度使用不同的搜索时间
    if difficulty == "easy":
        base_limit = 3.0
    elif difficulty == "hard":
        base_limit = MAX_AI_SEARCH_TIME
    else:  # normal
        base_limit = min(MAX_AI_SEARCH_TIME, 8.0)
    time_limit = min(base_limit, TIME_LIMIT - 1)

    # 初始解
    current_order = _initial_order()
    current_score, current_counts, current_edges = _score_order(current_order)
    if current_score is None:
        # 若初始解非法，退化为简单顺序
        current_order = list(range(NUM_RINGS))
        current_score, current_counts, current_edges = _score_order(current_order)

    if current_score is None:
        return {
            "finished": False,
            "valid": False,
            "score": 0,
            "counts": {"white": 0, "blue": 0, "green": 0, "red": 0},
            "edges": [],
            "submit_time": TIME_LIMIT,
        }

    best_order = current_order[:]
    best_score = current_score
    best_counts = current_counts
    best_edges = current_edges

    # 为“普通难度”保留一个次优解
    second_best_score = float("-inf")
    second_best_order = None
    second_best_counts = None
    second_best_edges = None

    T0 = 5.0
    Tmin = 0.1

    while True:
        now = time.time()
        elapsed = now - start
        if elapsed >= time_limit:
            break

        # 退火温度随时间线性下降
        frac = elapsed / time_limit
        T = max(Tmin, T0 * (1.0 - frac))

        cand_order = _neighbor(current_order)
        cand_score, cand_counts, cand_edges = _score_order(cand_order)
        if cand_score is None:
            continue

        delta = cand_score - current_score
        if delta >= 0 or math.exp(delta / max(T, 1e-6)) > random.random():
            # 接受新解
            current_order = cand_order
            current_score = cand_score
            current_counts = cand_counts
            current_edges = cand_edges

            if current_score > best_score:
                # 更新次优解为旧的最优解
                second_best_score = best_score
                second_best_order = best_order[:]
                second_best_counts = best_counts
                second_best_edges = best_edges

                best_order = current_order[:]
                best_score = current_score
                best_counts = current_counts
                best_edges = current_edges
            elif best_score > current_score > second_best_score:
                # 介于当前最优与次优之间，更新次优解
                second_best_score = current_score
                second_best_order = current_order[:]
                second_best_counts = current_counts
                second_best_edges = current_edges

    # 若搜索过程中没有找到更优/次优解，best_* 仍为初始解
    think_time = time.time() - start

    # 按难度选择解：
    # - hard：使用最优解，但提交时间为 100~250 秒的随机数（增加玩家可赢概率）
    # - normal：若存在次优解，则使用次优解，否则退回最优解，提交时间略长于搜索时间
    # - easy：使用较短时间搜索得到的最优解，提交时间略长于搜索时间
    # - normal：若存在次优解，则使用次优解，否则退回最优解
    # - easy：使用较短时间搜索得到的最优解（已由 time_limit 控制）
    if difficulty == "hard":
        # 强力解 + 很晚提交
        use_score = best_score
        use_counts = best_counts
        use_edges = best_edges
        submit_time = random.uniform(100.0, 250.0)
    elif difficulty == "normal" and second_best_edges is not None:
        use_score = second_best_score
        use_counts = second_best_counts
        use_edges = second_best_edges
        submit_time = min(TIME_LIMIT, think_time + random.uniform(5.0, 15.0))
    else:
        use_score = best_score
        use_counts = best_counts
        use_edges = best_edges
        submit_time = min(TIME_LIMIT, think_time + random.uniform(3.0, 8.0))

    # 普通难度下保存本局最优解，供展示环节使用
    if difficulty == "normal":
        state["best_solution_this_round"] = {
            "score": best_score,
            "counts": best_counts,
            "edges": best_edges,
        }
    else:
        state["best_solution_this_round"] = None

    return {
        "finished": True,
        "valid": True,
        "score": use_score,
        "counts": use_counts,
        "edges": use_edges,
        "submit_time": submit_time,
    }


def end_round():
    """结束当前一局，判定本局胜者，更新胜场；若有人达到 3 胜则整场结束，否则发 round_over 等玩家准备"""
    if state["game_over"]:
        return

    now = time.time()
    round_start = state.get("round_start_time") or now
    elapsed = now - round_start
    both_submitted_at = state.get("both_submitted_at")

    should_end_both = both_submitted_at is not None and (now - both_submitted_at) >= 2.5
    should_end_time = elapsed >= (TIME_LIMIT - 1)

    if not should_end_both and not should_end_time:
        remaining = max(1, TIME_LIMIT - elapsed)
        if state["game_timer"]:
            try:
                state["game_timer"].cancel()
            except Exception:
                pass
        state["game_timer"] = eventlet.spawn_after(remaining, end_round)
        return

    if both_submitted_at is not None:
        state["both_submitted_at"] = None
    state["game_over"] = True

    if state["game_timer"]:
        try:
            state["game_timer"].cancel()
        except Exception:
            pass
        state["game_timer"] = None

    r_h = state["result_human"] or {
        "finished": False,
        "valid": False,
        "score": 0,
        "counts": {"white": 0, "blue": 0, "green": 0, "red": 0},
    }
    r_ai = state["result_ai"] or {
        "finished": False,
        "valid": False,
        "score": 0,
        "counts": {"white": 0, "blue": 0, "green": 0, "red": 0},
    }

    h_done = r_h["finished"] and r_h["valid"]
    ai_done = r_ai["finished"] and r_ai["valid"]

    round_winner = None  # 先用字符串逻辑，稍后映射为 1/2
    if h_done and not ai_done:
        round_winner = "human"
    elif ai_done and not h_done:
        round_winner = "ai"
    elif h_done and ai_done:
        if r_h["score"] > r_ai["score"]:
            round_winner = "human"
        elif r_ai["score"] > r_h["score"]:
            round_winner = "ai"
        else:
            # 平局：用时短者获胜
            t_h = r_h.get("submit_time", float("inf"))
            t_ai = r_ai.get("submit_time", float("inf"))
            if t_h < t_ai:
                round_winner = "human"
            elif t_ai < t_h:
                round_winner = "ai"

    # 映射为前端使用的 1/2 标记
    if round_winner == "human":
        round_winner_numeric = 1
    elif round_winner == "ai":
        round_winner_numeric = 2
    else:
        round_winner_numeric = None

    if round_winner == "human":
        state["wins_human"] += 1
    elif round_winner == "ai":
        state["wins_ai"] += 1

    wins_h, wins_ai = state["wins_human"], state["wins_ai"]
    round_index = state["round_index"]

    # 是否整场结束
    if wins_h >= MATCH_WIN or wins_ai >= MATCH_WIN:
        match_winner = 1 if wins_h >= MATCH_WIN else 2
        match_payload = {
            "winner": match_winner,
            "wins_p1": wins_h,
            "wins_p2": wins_ai,
            "nickname_p1": state.get("nickname_human", "玩家"),
            "nickname_p2": state.get("nickname_ai", "AI"),
            "p1": r_h,
            "p2": r_ai,
            "difficulty": state.get("ai_difficulty", "normal"),
        }
        if state.get("best_solution_this_round") is not None:
            match_payload["best_solution"] = state["best_solution_this_round"]
        emit_to_human("match_over", match_payload)
        return

    # 否则等待玩家点“准备下一轮”
    state["ready"] = False
    payload = {
        "round_winner": round_winner_numeric,
        "round_index": round_index,
        "wins_p1": wins_h,
        "wins_p2": wins_ai,
        "nickname_p1": state.get("nickname_human", "玩家"),
        "nickname_p2": state.get("nickname_ai", "AI"),
        "p1": r_h,
        "p2": r_ai,
        "difficulty": state.get("ai_difficulty", "normal"),
    }
    if state.get("best_solution_this_round") is not None:
        payload["best_solution"] = state["best_solution_this_round"]
    emit_to_human("round_over", payload)


def start_round():
    """开始新一局：重置本局状态，生成新题板，给 AI 先算出一份答案，发 round_start"""
    state["edges_human"] = []
    state["edges_ai"] = []
    state["result_human"] = None
    state["result_ai"] = None
    state["best_solution_this_round"] = None
    state["game_over"] = False
    state["round_start_time"] = time.time()
    state["both_submitted_at"] = None

    generate_board()
    # 本局开始时直接为 AI 计算答案，不再等待预设提交时间
    ai_result = calc_ai_result()
    state["result_ai"] = ai_result
    state["edges_ai"] = ai_result.get("edges", [])

    # 启动本局定时器
    if state["game_timer"]:
        try:
            state["game_timer"].cancel()
        except Exception:
            pass
    state["game_timer"] = eventlet.spawn_after(TIME_LIMIT, end_round)

    emit_to_human(
        "round_start",
        {
            "round_index": state["round_index"],
            "wins_p1": state["wins_human"],
            "wins_p2": state["wins_ai"],
            "nickname_p1": state.get("nickname_human", "玩家"),
            "nickname_p2": state.get("nickname_ai", "AI"),
            "time_limit": TIME_LIMIT,
            "dots": state["dots"],
            "rings": state["rings"],
        },
    )


def start_game():
    """玩家加入后，初始化 5 局 3 胜并开始第 1 局"""
    state["round_index"] = 1
    state["wins_human"] = 0
    state["wins_ai"] = 0
    state["ready"] = False
    start_round()


@socketio.on("connect")
def on_connect():
    pass


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if state["human_sid"] == sid:
        state["human_sid"] = None


@socketio.on("join")
def on_join(data):
    """单人加入游戏，AI 始终在线"""
    raw_nick = (data.get("nickname") or "").strip()[:NICKNAME_MAX_LEN]
    diff = (data.get("difficulty") or "normal").strip().lower()
    if diff not in ("easy", "normal", "hard"):
        diff = "normal"
    state["human_sid"] = request.sid
    state["nickname_human"] = raw_nick or "玩家"
    state["nickname_ai"] = "AI"
    state["ai_difficulty"] = diff
    emit("joined", {"player": "human", "nickname": state["nickname_human"]})

    # 只有玩家一个人，直接开始整场游戏
    start_game()


@socketio.on("submit_polygon")
def on_submit_polygon(data):
    """玩家提交最终连线结果"""
    if state["game_over"]:
        return

    if request.sid != state["human_sid"]:
        return

    edges = data.get("edges") or []
    # 规范化边（小索引在前），去重
    norm_edges = set()
    for e in edges:
        if not isinstance(e, (list, tuple)) or len(e) != 2:
            continue
        a, b = int(e[0]), int(e[1])
        if a == b:
            continue
        if not (0 <= a < NUM_RINGS and 0 <= b < NUM_RINGS):
            continue
        if a > b:
            a, b = b, a
        norm_edges.add((a, b))
    edges_list = list(norm_edges)

    score, counts = compute_score_and_counts(edges_list)
    valid = score is not None

    # 非法多边形：立即告知玩家，不记录为本局结果，允许继续修改后再提交
    if not valid:
        emit(
            "submit_result",
            {
                "ok": False,
                "message": "多边形不合法（需 15 边形且无线段相交），请调整后重试。",
            },
        )
        return

    submit_time = time.time() - (state.get("round_start_time") or time.time())
    result = {
        "finished": True,
        "valid": True,
        "score": score,
        "counts": counts,
        "edges": edges_list,
        "submit_time": submit_time,
    }

    state["edges_human"] = edges_list
    state["result_human"] = result

    # 告知玩家：已成功提交（不展示分数）
    emit(
        "submit_result",
        {
            "ok": True,
            "message": "已提交合法 15 边形，等待本局结束。",
        },
    )

    # 玩家和 AI 均已有结果：3 秒后统一结算本局
    if state["result_human"] and state["result_ai"] and state["both_submitted_at"] is None:
        state["both_submitted_at"] = time.time()
        emit_to_human("round_finished", {})
        eventlet.spawn_after(3, end_round)


@socketio.on("ready")
def on_ready(data):
    """玩家点击“准备下一轮”"""
    if not state["game_over"]:
        return
    if request.sid != state["human_sid"]:
        return

    state["ready"] = True
    # 玩家一个人 ready 就够了，进入下一局
    state["round_index"] += 1
    start_round()


@app.route("/")
def index():
    # 复用同一个前端页面
    return send_from_directory("static", "polygon.html")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5002, debug=False)

