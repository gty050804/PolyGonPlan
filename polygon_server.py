# -*- coding: utf-8 -*-
"""
多边形连线对战游戏 - 服务端

规则简述：
- 画布上随机分布：
  - 16 个白点、8 个蓝点、2 个绿点、4 个红点
  - 15 个黑色空心小圆环（只允许在圆环圆心之间连线）
- 玩家在 5 分钟内，用线段依次连接 15 个圆环圆心，形成一个不自交的 15 边形回路：
  - 任意两条线段不能相交
  - 验证通过后，统计 15 边形内部的点数得分：
    - 白点：+1 分，蓝点：+2 分，绿点：+5 分，红点：-4 分
- 若一方未在时限内形成合法 15 边形而对方完成，则对方直接胜利；
- 若双方都完成，则比较得分，高者胜。
"""
import math
import random
import time
import eventlet

eventlet.monkey_patch()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = "polygon-battle-secret"
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

TIME_LIMIT = 300  # 总时间 5 分钟（秒）
MAX_ROUNDS = 50  # 每人最多尝试的边数/题量（上限，防御用）


MATCH_WIN = 3  # 5 局 3 胜

NICKNAME_MAX_LEN = 20

state = {
    "player1_sid": None,
    "player2_sid": None,
    "nickname_p1": "玩家1",
    "nickname_p2": "玩家2",
    # 点 & 圆环
    "dots": [],   # 每个元素: {"x":float,"y":float,"color":str}
    "rings": [],  # 每个元素: {"x":float,"y":float,"id":int}
    # 当前局每个玩家的连线和结果
    "edges_p1": [],
    "edges_p2": [],
    "result_p1": None,
    "result_p2": None,
    "game_timer": None,
    "game_over": False,
    # 5 局 3 胜
    "round_index": 1,   # 当前第几局 (1~5)
    "wins_p1": 0,
    "wins_p2": 0,
    "ready_p1": False,
    "ready_p2": False,
    "round_start_time": None,   # 本局开始时间（墙钟），用于防止定时器提前触发
    "both_submitted_at": None,  # 双方都提交时的时间，用于 3 秒后结束
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


def emit_to_both(event, data):
    p1, p2 = state["player1_sid"], state["player2_sid"]
    if p1:
        socketio.emit(event, data, room=p1)
    if p2:
        socketio.emit(event, data, room=p2)


def get_player_by_sid(sid):
    if sid == state["player1_sid"]:
        return 1
    if sid == state["player2_sid"]:
        return 2
    return None


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

    # 以下是共线情况（端点重合算接触，不算“严格相交”用于本题约束）
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
        return None, None  # 非合法 15 边形

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
        # 下一个顶点是“不是上一个”的那个
        nxt = neighbors[0] if neighbors[0] != prev else neighbors[1]
        if nxt == 0:
            break
        order.append(nxt)
        prev, cur = cur, nxt

    rings = state["rings"]
    poly_pts = [(rings[i]["x"], rings[i]["y"]) for i in order]

    # 统计各颜色点数与得分
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


def end_round():
    """结束当前一局，判定本局胜者，更新胜场；若有人达到 3 胜则整场结束，否则发 round_over 等双方准备"""
    if state["game_over"]:
        return

    now = time.time()
    round_start = state.get("round_start_time") or now
    elapsed = now - round_start
    both_submitted_at = state.get("both_submitted_at")

    # 仅在以下两种情况之一时真正结束：(1) 双方已提交且已过 3 秒；(2) 实际已到 5 分钟
    # 否则视为定时器提前触发，重新调度剩余时间
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

    r1 = state["result_p1"] or {"finished": False, "valid": False, "score": 0, "counts": {"white": 0, "blue": 0, "green": 0, "red": 0}}
    r2 = state["result_p2"] or {"finished": False, "valid": False, "score": 0, "counts": {"white": 0, "blue": 0, "green": 0, "red": 0}}

    p1_done = r1["finished"] and r1["valid"]
    p2_done = r2["finished"] and r2["valid"]

    round_winner = None
    if p1_done and not p2_done:
        round_winner = 1
    elif p2_done and not p1_done:
        round_winner = 2
    elif p1_done and p2_done:
        if r1["score"] > r2["score"]:
            round_winner = 1
        elif r2["score"] > r1["score"]:
            round_winner = 2
        else:
            # 平局：用时短者获胜
            t1 = r1.get("submit_time", float("inf"))
            t2 = r2.get("submit_time", float("inf"))
            if t1 < t2:
                round_winner = 1
            elif t2 < t1:
                round_winner = 2

    if round_winner == 1:
        state["wins_p1"] += 1
    elif round_winner == 2:
        state["wins_p2"] += 1

    wins_p1, wins_p2 = state["wins_p1"], state["wins_p2"]
    round_index = state["round_index"]

    if wins_p1 >= MATCH_WIN or wins_p2 >= MATCH_WIN:
        match_winner = 1 if wins_p1 >= MATCH_WIN else 2
        emit_to_both(
            "match_over",
            {
                "winner": match_winner,
                "wins_p1": wins_p1,
                "wins_p2": wins_p2,
                "nickname_p1": state.get("nickname_p1", "玩家1"),
                "nickname_p2": state.get("nickname_p2", "玩家2"),
                "p1": r1,
                "p2": r2,
            },
        )
        return

    state["ready_p1"] = False
    state["ready_p2"] = False
    emit_to_both(
        "round_over",
        {
            "round_winner": round_winner,
            "round_index": round_index,
            "wins_p1": wins_p1,
            "wins_p2": wins_p2,
            "nickname_p1": state.get("nickname_p1", "玩家1"),
            "nickname_p2": state.get("nickname_p2", "玩家2"),
            "p1": r1,
            "p2": r2,
        },
    )


def start_round():
    """开始新一局：重置本局状态，生成新题板，发 round_start"""
    state["edges_p1"] = []
    state["edges_p2"] = []
    state["result_p1"] = None
    state["result_p2"] = None
    state["game_over"] = False
    state["round_start_time"] = time.time()
    state["both_submitted_at"] = None

    generate_board()

    if state["game_timer"]:
        try:
            state["game_timer"].cancel()
        except Exception:
            pass
    state["game_timer"] = eventlet.spawn_after(TIME_LIMIT, end_round)

    emit_to_both(
        "round_start",
        {
            "round_index": state["round_index"],
            "wins_p1": state["wins_p1"],
            "wins_p2": state["wins_p2"],
            "nickname_p1": state.get("nickname_p1", "玩家1"),
            "nickname_p2": state.get("nickname_p2", "玩家2"),
            "time_limit": TIME_LIMIT,
            "dots": state["dots"],
            "rings": state["rings"],
        },
    )


def start_game():
    """双方加入后，初始化 5 局 3 胜并开始第 1 局"""
    state["round_index"] = 1
    state["wins_p1"] = 0
    state["wins_p2"] = 0
    state["ready_p1"] = False
    state["ready_p2"] = False
    start_round()


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
    raw_nick = (data.get("nickname") or "").strip()[:NICKNAME_MAX_LEN]
    if player == 1:
        state["player1_sid"] = request.sid
        state["nickname_p1"] = raw_nick or "玩家1"
    elif player == 2:
        state["player2_sid"] = request.sid
        state["nickname_p2"] = raw_nick or "玩家2"
    emit("joined", {"player": player})

    if state["player1_sid"] and state["player2_sid"]:
        start_game()


@socketio.on("submit_polygon")
def on_submit_polygon(data):
    """玩家提交最终连线结果"""
    if state["game_over"]:
        return

    player = get_player_by_sid(request.sid)
    if player is None:
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

    # 非法多边形：立即告知该玩家，不记录为最终结果，允许其继续修改后再提交
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
        "submit_time": submit_time,  # 本局提交用时（秒），平局时用时短者胜
    }

    if player == 1:
        state["edges_p1"] = edges_list
        state["result_p1"] = result
    else:
        state["edges_p2"] = edges_list
        state["result_p2"] = result

    # 告知该玩家：已成功提交（不展示分数）
    emit(
        "submit_result",
        {
            "ok": True,
            "message": "已提交合法 15 边形，等待对手或时间结束。",
        },
    )

    if state["result_p1"] and state["result_p2"]:
        state["both_submitted_at"] = time.time()
        emit_to_both("round_finished", {})
        eventlet.spawn_after(3, end_round)


@socketio.on("ready")
def on_ready(data):
    """玩家点击“准备下一轮”"""
    if state["game_over"] is False:
        return
    player = get_player_by_sid(request.sid)
    if player is None:
        return
    if player == 1:
        state["ready_p1"] = True
    else:
        state["ready_p2"] = True
    if state["ready_p1"] and state["ready_p2"]:
        state["round_index"] += 1
        start_round()


@app.route("/")
def index():
    # 提供新的前端页面
    return send_from_directory("static", "polygon.html")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5001, debug=False)

