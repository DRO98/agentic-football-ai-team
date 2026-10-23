"""AFC (2026-10-22): translate between the match server's pitch and the pitch our agents are written for.

Found in the first real match (78 GK captures, afc_tools/fetch_match_logs.py):
  - The real pitch is about 8.6x smaller than the sample: players never leave x in [-6.4, 6.4], and the
    lateral coordinate stays within about +-3.6. Prompts, rules, fallbacks and the validator all assume
    the sample's x -55..55, y -35..35.
  - The ball's lateral coordinate is position.z; its position.y is the HEIGHT (0.1-1.3). Players carry
    only x and y, with y lateral. Reading the ball's (x, y) put it on the centre line laterally, so
    possession was UNKNOWN on 45 of 70 open-play ticks and OURS only once.
  - The server executes MOVE_TO in its own units: our GK asked for x -55 and stood at -5.93 (the goal
    line). Targets such as (25, 15) were clamped to the corners, so players sprinted to the edges.

So: the incoming state is scaled into the design frame (x and lateral times SCALE, ball lateral from
z), everything downstream runs unchanged, and outgoing coordinates and distances are divided back.
The sample and every test state (GKs near +-50) are detected as the design frame and left untouched.
"""

import copy
import math

DESIGN_HALF_LENGTH = 55.0
SERVER_HALF_LENGTH = 6.4               # observed: players clamped at x = +-6.4; the ball crosses to 6.9 in the goal
SCALE = DESIGN_HALF_LENGTH / SERVER_HALF_LENGTH
SERVER_MAX_X = 8.0                     # a state whose players and ball all stay within this is the server's frame
SERVER_HALF_WIDTH = 35.0 / SCALE       # 4.07, the design pitch's 110 x 70 ratio; observed lateral max 3.6
DISTANCE_PARAMS = {"SLIDE_TACKLE": ("distance",), "FOLLOW_PLAYER": ("distance",)}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def is_server_frame(game_state: dict) -> bool:
    """True when every player x and the ball x are finite and within SERVER_MAX_X."""
    if not isinstance(game_state, dict):
        return False
    players = game_state.get("players") or []
    ball = (game_state.get("ball") or {}).get("position") or {}
    xs = [(p.get("position") or {}).get("x") for p in players if isinstance(p, dict)] + [ball.get("x")]
    return bool(players) and all(_num(x) for x in xs) and max(abs(x) for x in xs) <= SERVER_MAX_X


def to_design_frame(game_state: dict) -> tuple[dict, float]:
    """(state in the design frame, scale). scale 1.0 means the state was already in the design frame."""
    if not is_server_frame(game_state):
        return game_state, 1.0
    gs = copy.deepcopy(game_state)
    for p in gs["players"]:
        pos = p.get("position") or {}
        p["position"] = {k: (v * SCALE if _num(v) and k in ("x", "y") else v) for k, v in pos.items()}
    ball = gs.get("ball") or {}
    for key in ("position", "velocity"):
        v = ball.get(key)
        if isinstance(v, dict) and _num(v.get("x")) and _num(v.get("z")):
            ball[key] = {"x": v["x"] * SCALE, "y": v["z"] * SCALE, "z": v.get("y")}  # lateral <- z, height -> z
    return gs, SCALE


def to_server_frame(commands, scale: float):
    """Divide outgoing coordinates and distances by `scale`; MOVE_TO is kept inside the server pitch."""
    if scale == 1.0 or not isinstance(commands, list):
        return commands
    out = []
    for c in commands:
        c = copy.deepcopy(c)
        par = c.get("parameters") if isinstance(c, dict) else None
        if isinstance(par, dict):
            if c.get("commandType") == "MOVE_TO":
                if _num(par.get("target_x")):
                    par["target_x"] = round(max(-SERVER_HALF_LENGTH, min(SERVER_HALF_LENGTH, par["target_x"] / scale)), 2)
                if _num(par.get("target_y")):
                    par["target_y"] = round(max(-SERVER_HALF_WIDTH, min(SERVER_HALF_WIDTH, par["target_y"] / scale)), 2)
            for k in DISTANCE_PARAMS.get(c.get("commandType"), ()):
                if _num(par.get(k)):
                    par[k] = round(par[k] / scale, 2)
        out.append(c)
    return out


# AFC (2026-10-22, match 13: our first match as team 1, lost 0-3): with our ball the forwards ran backwards.
# Replayed on that match's states, Nova Lite's MOVE_TO targets went backwards for FWD2 7/8 (median -20 design
# units) and FWD1 4/5 as team 1, but 2/8 and 2/4 on the same states turned 180 degrees and presented as team 0:
# the models assume "forward = +x". So team 1 always plays a home view: the state is rotated 180 degrees
# (x, y -> -x, -y; home/away swapped) and everything runs as team 0; replies are rotated back.
# A rotation, not a reflection: left/right as seen by a player is unchanged.
_SWAP_CODE = {"home": "away", "away": "home"}
_SWAP_TEAM = {"HOME": "AWAY", "AWAY": "HOME"}


def _neg_xy(v):
    if not isinstance(v, dict):
        return v
    return {k: (-x if k in ("x", "y") and _num(x) else x) for k, x in v.items()}


def to_home_view(game_state: dict, team_id) -> tuple[dict, bool]:
    """(state as seen by team 0, flipped). Only team 1 is flipped; anything else is returned unchanged."""
    if team_id != 1 or not isinstance(game_state, dict):
        return game_state, False
    gs = copy.deepcopy(game_state)
    for p in gs.get("players") or []:
        if not isinstance(p, dict):
            continue
        for key in ("position", "velocity"):
            if key in p:
                p[key] = _neg_xy(p[key])
        if p.get("teamCode") in _SWAP_CODE:
            p["teamCode"] = _SWAP_CODE[p["teamCode"]]
        if p.get("teamId") in (0, 1):
            p["teamId"] = 1 - p["teamId"]
    ball = gs.get("ball")
    if isinstance(ball, dict):
        for key in ("position", "velocity"):
            if key in ball:
                ball[key] = _neg_xy(ball[key])
    score = gs.get("score")
    if isinstance(score, dict) and "home" in score and "away" in score:
        gs["score"] = {**score, "home": score["away"], "away": score["home"]}
    if gs.get("modeTeamId") in (0, 1):
        gs["modeTeamId"] = 1 - gs["modeTeamId"]
    return gs, True


def from_home_view(commands, flipped: bool, team_id):
    """Undo to_home_view on outgoing commands: rotate MOVE_TO back, swap FOLLOW_PLAYER's team, real teamId."""
    if not flipped or not isinstance(commands, list):
        return commands
    out = []
    for c in commands:
        if not isinstance(c, dict):
            out.append(c)
            continue
        c = copy.deepcopy(c)
        par = c.get("parameters")
        if isinstance(par, dict):
            if c.get("commandType") == "MOVE_TO":
                for k in ("target_x", "target_y"):
                    if _num(par.get(k)):
                        par[k] = -par[k]
            if c.get("commandType") == "FOLLOW_PLAYER" and par.get("target_team") in _SWAP_TEAM:
                par["target_team"] = _SWAP_TEAM[par["target_team"]]
        c["teamId"] = team_id
        out.append(c)
    return out
