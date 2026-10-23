"""AFC: deterministic pre-model rules (tournament copy only, not in the lab).

A rule looks at the tick before the model is called. When it fires, its command answers the
tick (after the same validator the model's reply goes through) and the model is not called,
which saves the call's latency and cost. When it does not fire, it returns None and the tick
goes to the model as before.

Why rules at all: the GK prompt says "If ball is loose near your box (summary says 'held by
free') -> INTERCEPT", and in the deployed team run of 2026-10-18 Nova Micro ignored it for a
loose ball 8 units from the keeper (it answered MOVE_TO to its goal line). The fallback already
does INTERCEPT there; this rule makes it the answer instead of the model's choice.

DEF (2026-10-22): with an outfield teammate on the ball, hold a central spot behind the attack
(def_shape_on_our_ball, below) instead of the model pressing or passing our own ball.
"""

import math

from action_schema import GK_ID
from fallback import GK_CONFIG, _cmd
from possession import Possession, resolve_possession
from state import _is_my_team, _player_idx, _real_xy, get_goal_positions

# ASSUMPTION, not server-verified: a standard penalty area in pitch units (the pitch is
# x -55..55, y -35..35, i.e. 110 x 70, standard metres). Depth from the goal line, half width.
PENALTY_AREA_DEPTH = 16.5
PENALTY_AREA_HALF_WIDTH = 20.16


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def ball_in_own_penalty_area(ball_pos: dict, team_id: int) -> bool:
    x, y = ball_pos.get("x"), ball_pos.get("y")
    if not (_finite(x) and _finite(y)):
        return False
    my_goal_x, _ = get_goal_positions(team_id)
    inward = (x - my_goal_x) if my_goal_x < 0 else (my_goal_x - x)  # distance from my goal line, into the pitch
    return 0.0 <= inward <= PENALTY_AREA_DEPTH and abs(y) <= PENALTY_AREA_HALF_WIDTH


def gk_loose_ball_in_box(game_state: dict, team_id: int, player_id: int):
    """GK only: a LOOSE ball inside our own penalty area -> INTERCEPT. Otherwise None."""
    if player_id != GK_ID:
        return None
    ball = game_state.get("ball") or {}
    info = resolve_possession(ball, game_state.get("players") or [], team_id)
    if info.state is not Possession.LOOSE:  # OURS / THEIRS / UNKNOWN: leave it to the model
        return None
    if not ball_in_own_penalty_area(ball.get("position") or {}, team_id):
        return None
    return [_cmd("INTERCEPT", player_id, team_id, {"aggressive": False}, duration=GK_CONFIG.press_duration)]


# AFC (2026-10-22): DEF shape on our ball. In model_eval-def-20261022-124954 (Nova Lite, 4 situations x 4
# reps x 3 prompts) DEF answered our own ball with PRESS_BALL or a PASS it could not make in 31 of 48
# replies (all rejected -> fallback), and the prompted shape rule was followed in 10 of 16 at best.
DEF_ID = 1
DEF_SHAPE_OFFSET = 15.0  # pitch units closer to our goal than our most advanced outfield teammate


def def_shape_on_our_ball(game_state: dict, team_id: int, player_id: int):
    """DEF only: an outfield teammate holds the ball -> MOVE_TO a central spot (y 0) DEF_SHAPE_OFFSET
    behind our most advanced outfield teammate, never past halfway (x 0). Otherwise None: their
    ball, a loose or unknown ball, our GK or DEF itself on the ball all go to the model."""
    if player_id != DEF_ID:
        return None
    ball = game_state.get("ball") or {}
    players = game_state.get("players") or []
    info = resolve_possession(ball, players, team_id)
    if info.state is not Possession.OURS or info.holder_idx in (GK_ID, player_id):
        return None
    xs = []
    for p in players:
        if _is_my_team(p, team_id) and _player_idx(p) not in (GK_ID, player_id):
            xy = _real_xy(p.get("position"))
            if xy:
                xs.append(xy[0])
    if not xs:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    toward = 1 if opp_goal_x > 0 else -1
    target_x = max(xs, key=lambda x: x * toward) - DEF_SHAPE_OFFSET * toward
    if target_x * toward > 0:  # past halfway
        target_x = 0.0
    return [_cmd("MOVE_TO", player_id, team_id, {"target_x": round(target_x, 1), "target_y": 0.0, "sprint": False})]


# AFC (2026-10-22): stamina. In the first real match our forwards played at median stamina 0.16 and 0.11
# (opponents 0.47), sprinting on ~25 % of ticks; Nova Lite sets sprint: true on almost every MOVE_TO.
SPRINT_MIN_STAMINA = 0.5    # was 0.35; match 11: we sprinted on 29 % of player-ticks vs their 19 %, MID hit 0 by half time


def limit_sprint(commands, game_state: dict, team_id: int, player_id: int):
    """Turn `sprint` off when my stamina (0-1) is below SPRINT_MIN_STAMINA. Unknown stamina: unchanged."""
    me = next((p for p in (game_state.get("players") or [])
               if isinstance(p, dict) and _is_my_team(p, team_id) and _player_idx(p) == player_id), None)
    stamina = me.get("stamina") if me else None
    if not (_finite(stamina) and 0.0 <= stamina <= 1.0 and stamina < SPRINT_MIN_STAMINA) or not isinstance(commands, list):
        return commands
    out = []
    for c in commands:
        if isinstance(c, dict) and isinstance(c.get("parameters"), dict) and c["parameters"].get("sprint") is True:
            c = {**c, "parameters": {**c["parameters"], "sprint": False}}
        out.append(c)
    return out


# AFC (2026-10-22, after matches 2 and 3; captures in C:\dev\afc\captures\match2.log):
#  - Our models almost never acted on the ball (3 SHOOT, ~8 PASS in three matches): possession was UNKNOWN
#    (fixed by lib/frame.py), and the forwards' prompt forbids shots above distOppGoal 25 (~2.9 m real).
#  - Shots fly at ~15 m/s: they cross the 12.8 m pitch in under a second, while we get a state every 2 s,
#    so no keeper of ours can react. Goals against came from 9.5 and 11.4 m. -> shoot from their half.
#  - One goal against came after our GK's model sent it to the other goal (MOVE_TO x 6.4).
SHOOT_RANGE = 55.0           # design units = their half: shoot whenever I hold the ball within this of their goal
POACHER_ID = 4               # FWD2: stays forward and open instead of pressing
POACHER_X = 0.7              # x as a fraction of the opponent goal x when we have the ball (38.5 = ~4.5 m real)
POACHER_Y = 12.0             # lateral offset, on the side away from the ball
POACHER_WAIT_X = 0.2         # x fraction while they have the ball: just inside their half, ready for a steal
POACHER_PRESS_RADIUS = 15.0  # their ball this close to the poacher: leave it to the model (press/intercept)
GK_MAX_DEPTH = PENALTY_AREA_DEPTH       # the GK's MOVE_TO never leaves our penalty area
GK_MAX_WIDTH = PENALTY_AREA_HALF_WIDTH


def _me(game_state, team_id, player_id):
    return next((p for p in (game_state.get("players") or [])
                 if isinstance(p, dict) and _is_my_team(p, team_id) and _player_idx(p) == player_id), None)


def chain(*rules):
    """The first rule that answers wins; None when none does. Keeps each rule's name for the logs."""
    def chained(game_state, team_id, player_id):
        for r in rules:
            out = r(game_state, team_id, player_id)
            if out:
                chained.__name__ = r.__name__
                return out
        return None
    chained.__name__ = "+".join(r.__name__ for r in rules)
    return chained


# AFC (2026-10-22, match 4): the goal mouth is narrow. Every goal in four matches crossed the line within
# 0.8 m of the centre (= ~7 design units), and the forwards hit the post a lot aiming at low corners.
# Their keeper stood more than 0.35 m (3 design units) off the centre on 73 % of open-play ticks.
GOAL_HALF_WIDTH = 7.0        # design units
ANGLE_SLOPE = 0.5            # shoot when |my lateral| <= GOAL_HALF_WIDTH + ANGLE_SLOPE * my distance to their goal line
CUT_INSIDE_STEP = 8.0        # otherwise dribble this far toward the goal, aiming at the centre line
# AFC (2026-10-22, match 6, lost 2-3): the opponents' #0 often leaves its goal - in six matches it stood more
# than 4 m from its own goal line on about half of the open-play ticks (often on OUR goal line, once scoring
# from there). All four BR shots in match 6 went wide right with that keeper near the halfway line: the old
# "centred keeper -> corner" test looked at its lateral position only.
KEEPER_OUT = 26.0            # design units (~3 m real) from their goal line: the goal counts as empty
EMPTY_GOAL_RANGE = 80.0      # with an empty goal, act from up to here (~9.3 m real; their goals came from 9.5-11.4 m)
# AFC (2026-10-22, match 14, won 4-1; matches 4-14: 53 rule shots, 8 goals): empty-goal shots scored no better than
# shots at a keeper in goal (4/24 vs 4/29), and those from 8-10 m all missed in match 14 (0/3). With the keeper
# out there is time: beyond EMPTY_GOAL_SHOOT_FROM, dribble toward the goal first and shoot from closer.
EMPTY_GOAL_SHOOT_FROM = 60.0  # design units (~7 m real)
EMPTY_GOAL_STEP = 12.0        # dribble this far per tick toward the goal, halving the lateral
# AFC (2026-10-22, match 7, won 3-2): power 1.0 sent shots over the bar (one ended behind the goal line between
# the posts without a goal), and the rule also fired from the centre spot at kick-off/reset. Power now grows with
# distance (design units to their goal line), and the rule only shoots in OPEN_PLAY.
SHOT_POWER_BY_DISTANCE = ((30.0, 0.75), (55.0, 0.85), (float("inf"), 0.95))


def shot_power(dist_x: float) -> float:
    return next(p for limit, p in SHOT_POWER_BY_DISTANCE if dist_x <= limit)


def carrier_shoot(game_state: dict, team_id: int, player_id: int):
    """Any outfield player holding our ball within SHOOT_RANGE of their goal (EMPTY_GOAL_RANGE when their
    keeper is more than KEEPER_OUT from its goal line: then always CENTER), OPEN_PLAY only, power by distance:
      - with a shooting angle -> SHOOT at CENTER, or the far low corner from wider than the posts (shot_aim);
      - from a tight angle (out wide, close to the line) -> MOVE_TO inward to open the angle first.
    Otherwise None."""
    if player_id == GK_ID or game_state.get("playMode", "OPEN_PLAY") != "OPEN_PLAY":
        return None
    ball = game_state.get("ball") or {}
    info = resolve_possession(ball, game_state.get("players") or [], team_id)
    if info.state is not Possession.OURS or info.holder_idx != player_id:
        return None
    me = _real_xy((_me(game_state, team_id, player_id) or {}).get("position"))
    if not me:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    dist_x = abs(me[0] - opp_goal_x)
    keeper = next((p for p in game_state.get("players") or [] if isinstance(p, dict)
                   and not _is_my_team(p, team_id) and _player_idx(p) == GK_ID), None)
    kxy = _real_xy((keeper or {}).get("position"))
    empty_goal = kxy is not None and abs(kxy[0] - opp_goal_x) > KEEPER_OUT
    if dist_x > (EMPTY_GOAL_RANGE if empty_goal else SHOOT_RANGE):
        return None
    toward = 1.0 if opp_goal_x > 0 else -1.0
    if abs(me[1]) > GOAL_HALF_WIDTH + ANGLE_SLOPE * dist_x:
        step_x = min(CUT_INSIDE_STEP, max(0.0, dist_x - 4.0))
        return [_cmd("MOVE_TO", player_id, team_id, {"target_x": round(me[0] + toward * step_x, 1),
                                                      "target_y": round(me[1] * 0.4, 1), "sprint": False})]
    # AFC (2026-10-22, matches 4-11, 38 rule shots in open play): CENTER scored 4/21, BL/BR 1/17 - corner
    # shots ended 2.8-3.6 m off the centre of a goal ~0.8 m either side. CENTER between the posts (shot_aim).
    if empty_goal and dist_x > EMPTY_GOAL_SHOOT_FROM:
        return [_cmd("MOVE_TO", player_id, team_id, {"target_x": round(me[0] + toward * EMPTY_GOAL_STEP, 1),
                                                      "target_y": round(me[1] * 0.5, 1), "sprint": False})]
    return [_cmd("SHOOT", player_id, team_id, {"aim_location": shot_aim(me, toward), "power": shot_power(dist_x)})]


# AFC (2026-10-23, user report): shooting from out wide, the ball keeps hitting the side netting from both sides.
# If the server aims at a point inside the goal (behind the line), from a tight angle the path crosses the goal
# line outside the near post; aimed at the far post it crosses inside. Data (rule shots, matches 4-19): from
# wider than the posts 6/27 goals, between them 5/35 - close, wide shots often ended right next to the shooter.
def shot_aim(me, toward: float) -> str:
    """CENTER between the posts; from wider than the goal mouth, the low FAR corner. L/R are as the shooter faces
    their goal: +lateral is the shooter's left for team 0 (BL shots ended on that side), so on the left -> BR."""
    if abs(me[1]) <= GOAL_HALF_WIDTH:
        return "CENTER"
    return "BR" if me[1] * toward > 0 else "BL"


def fwd2_poacher(game_state: dict, team_id: int, player_id: int):
    """FWD2 only. Our ball (someone else holds it) -> get open near their goal, away from the ball.
    Their ball far from me -> wait just inside their half instead of pressing. Otherwise None (the model)."""
    if player_id != POACHER_ID:
        return None
    ball = game_state.get("ball") or {}
    info = resolve_possession(ball, game_state.get("players") or [], team_id)
    me = _real_xy((_me(game_state, team_id, player_id) or {}).get("position"))
    bxy = _real_xy(ball.get("position"))
    if not me or not bxy:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    side = -1.0 if bxy[1] > 0 else 1.0  # away from the ball
    if info.state is Possession.OURS and info.holder_idx != player_id:
        target = (opp_goal_x * POACHER_X, side * POACHER_Y)
    elif info.state is Possession.THEIRS and math.hypot(bxy[0] - me[0], bxy[1] - me[1]) > POACHER_PRESS_RADIUS:
        target = (opp_goal_x * POACHER_WAIT_X, side * POACHER_Y * 0.5)
    else:
        return None
    far = math.hypot(target[0] - me[0], target[1] - me[1]) > 15.0
    return [_cmd("MOVE_TO", player_id, team_id, {"target_x": round(target[0], 1), "target_y": round(target[1], 1),
                                                  "sprint": far})]


# AFC (2026-10-22, match 8 vs a human team, won 2-0): FWD1 had the lowest stamina (median 0.29) and pressed our
# own carrier (rejected own_ball 5x in one match, 3x in match 7); with our ball it had no job.
FWD1_ID = 3
SUPPORT_AHEAD = 15.0         # ahead of the carrier toward their goal (design units)
SUPPORT_MAX_X = 0.6          # never past this fraction of their goal x (FWD2 is the one near the goal)
SUPPORT_Y = 0.5              # lateral: half the carrier's lateral, so a pass inside stays short


def fwd1_support(game_state: dict, team_id: int, player_id: int):
    """FWD1 only: an outfield teammate holds our ball -> MOVE_TO ahead of the ball, toward the centre, as a
    passing/rebound option instead of pressing our own carrier. Otherwise None (the model)."""
    if player_id != FWD1_ID:
        return None
    ball = game_state.get("ball") or {}
    info = resolve_possession(ball, game_state.get("players") or [], team_id)
    if info.state is not Possession.OURS or info.holder_idx in (GK_ID, player_id):
        return None
    me = _real_xy((_me(game_state, team_id, player_id) or {}).get("position"))
    bxy = _real_xy(ball.get("position"))
    if not me or not bxy:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    toward = 1.0 if opp_goal_x > 0 else -1.0
    tx = bxy[0] + toward * SUPPORT_AHEAD
    cap = opp_goal_x * SUPPORT_MAX_X
    tx = min(tx, cap) if toward > 0 else max(tx, cap)
    ty = bxy[1] * SUPPORT_Y
    far = math.hypot(tx - me[0], ty - me[1]) > 15.0
    return [_cmd("MOVE_TO", player_id, team_id, {"target_x": round(tx, 1), "target_y": round(ty, 1), "sprint": far})]


# AFC (2026-10-22, match 15, won 1-0): the GK (prompt and fallback: "THROW to the nearest teammate") threw 3/3
# distributions to DEF #1 inside our box with opponents 0.2-0.6 m away; possession turned over each time, while
# FWD2 stood free near halfway (nearest opponent 1.7-2.6 m).
OPEN_RADIUS = 13.0           # design units (~1.5 m real): a teammate with an opponent closer than this is marked
KICK_FROM = 40.0             # targets further than this from the GK get a KICK, nearer ones a THROW


def gk_distribute(game_state: dict, team_id: int, player_id: int):
    """GK only, holding our ball: GK_DISTRIBUTE to the most advanced open outfield teammate (no opponent within
    OPEN_RADIUS); if nobody is open, to the least marked one. KICK when the target is far, else THROW."""
    if player_id != GK_ID:
        return None
    players = game_state.get("players") or []
    info = resolve_possession(game_state.get("ball") or {}, players, team_id)
    if info.state is not Possession.OURS or info.holder_idx != GK_ID:
        return None
    me = _real_xy((_me(game_state, team_id, player_id) or {}).get("position"))
    if not me:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    toward = 1.0 if opp_goal_x > 0 else -1.0
    opponents = [xy for xy in (_real_xy(p.get("position")) for p in players
                               if isinstance(p, dict) and not _is_my_team(p, team_id)) if xy]
    cands = []
    for p in players:
        if not isinstance(p, dict) or not _is_my_team(p, team_id) or _player_idx(p) == GK_ID:
            continue
        xy = _real_xy(p.get("position"))
        if not xy:
            continue
        free = min((math.hypot(xy[0] - o[0], xy[1] - o[1]) for o in opponents), default=99.0)
        cands.append((_player_idx(p), xy, free))
    if not cands:
        return None
    open_ = [c for c in cands if c[2] >= OPEN_RADIUS]
    idx, xy, _ = (max(open_, key=lambda c: c[1][0] * toward) if open_ else max(cands, key=lambda c: c[2]))
    far = math.hypot(xy[0] - me[0], xy[1] - me[1]) > KICK_FROM
    return [_cmd("GK_DISTRIBUTE", player_id, team_id, {"target_player_id": idx, "method": "KICK" if far or not open_ else "THROW"})]


# AFC (2026-10-23, match 19, won 3-2; user request): with the ball in OUR half, do not carry it (and never
# backwards) - release it: pass up to a free teammate ahead, or, with nobody free ahead and 2+ opponents in
# front, a hard long shot "to the top corner". In match 19 the empty-goal rule made carriers dribble out of
# our own half with 4-5 opponents ahead, and 3 of 13 own-half carries had a free teammate ahead.
# Aim letters: in matches 8-14 BL shots ended on the shooter's +lateral side for team 0 (L = left as the
# shooter faces their goal); top corners have no data yet (corners overall 1/17 vs CENTER 7/36).
RELEASE_AHEAD = 5.0           # a teammate counts as "ahead" this far in front of the carrier (design units)
LONG_PASS = 35.0              # longer passes go AERIAL, shorter ones GROUND
RELEASE_MIN_OPPONENTS = 2     # long shot only with at least this many OUTFIELD opponents in front of the carrier
NO_BACK_STEP = 10.0           # a backward carry in our half becomes a step this far forward
BACKWARD_TOLERANCE = 3.0


def _carrier_in_own_half(game_state, team_id, player_id):
    """(my xy, toward, opponents xy) when I (outfield) hold our ball in our own half in open play, else None."""
    if player_id == GK_ID or game_state.get("playMode", "OPEN_PLAY") != "OPEN_PLAY":
        return None
    players = game_state.get("players") or []
    info = resolve_possession(game_state.get("ball") or {}, players, team_id)
    if info.state is not Possession.OURS or info.holder_idx != player_id:
        return None
    me = _real_xy((_me(game_state, team_id, player_id) or {}).get("position"))
    if not me:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    toward = 1.0 if opp_goal_x > 0 else -1.0
    if me[0] * toward > 0:
        return None
    opponents = [xy for xy in (_real_xy(p.get("position")) for p in players
                               if isinstance(p, dict) and not _is_my_team(p, team_id)) if xy]
    return me, toward, opponents


def own_half_release(game_state: dict, team_id: int, player_id: int):
    """Carrier in our own half: PASS to the most advanced free teammate ahead (GROUND if near, AERIAL if far);
    with nobody free ahead and RELEASE_MIN_OPPONENTS+ opponents in front, SHOOT hard at the top corner away from
    their keeper (CENTER if their goal is empty). Otherwise None (the model decides)."""
    ctx = _carrier_in_own_half(game_state, team_id, player_id)
    if not ctx:
        return None
    me, toward, opponents = ctx
    players = game_state.get("players") or []
    ahead = []
    for p in players:
        if not isinstance(p, dict) or not _is_my_team(p, team_id) or _player_idx(p) in (GK_ID, player_id):
            continue
        xy = _real_xy(p.get("position"))
        if not xy or (xy[0] - me[0]) * toward < RELEASE_AHEAD:
            continue
        if min((math.hypot(xy[0] - o[0], xy[1] - o[1]) for o in opponents), default=99.0) >= OPEN_RADIUS:
            ahead.append((_player_idx(p), xy))
    if ahead:
        idx, xy = max(ahead, key=lambda c: c[1][0] * toward)
        kind = "GROUND" if math.hypot(xy[0] - me[0], xy[1] - me[1]) <= LONG_PASS else "AERIAL"
        return [_cmd("PASS", player_id, team_id, {"target_player_id": idx, "type": kind})]
    outfield_ahead = 0                            # their keeper does not count
    for p in players:
        if isinstance(p, dict) and not _is_my_team(p, team_id) and _player_idx(p) != GK_ID:
            xy = _real_xy(p.get("position"))
            if xy and (xy[0] - me[0]) * toward > 0:
                outfield_ahead += 1
    if outfield_ahead < RELEASE_MIN_OPPONENTS:
        return None
    _, opp_goal_x = get_goal_positions(team_id)
    keeper = next((p for p in players if isinstance(p, dict) and not _is_my_team(p, team_id)
                   and _player_idx(p) == GK_ID), None)
    kxy = _real_xy((keeper or {}).get("position"))
    if kxy is None or abs(kxy[0] - opp_goal_x) > KEEPER_OUT:
        aim = "CENTER"
    else:
        keeper_on_my_left = kxy[1] * toward > 0      # +lateral is the shooter's left for team 0 (see above)
        aim = "TR" if keeper_on_my_left else "TL"
    return [_cmd("SHOOT", player_id, team_id, {"aim_location": aim, "power": 1.0})]


def no_backward_carry(commands, game_state: dict, team_id: int, player_id: int):
    """Output filter: while I carry our ball in our own half, a MOVE_TO going backwards becomes a short step
    forward (lateral pulled toward the middle). Everything else passes unchanged."""
    if not isinstance(commands, list):
        return commands
    ctx = _carrier_in_own_half(game_state, team_id, player_id)
    if not ctx:
        return commands
    me, toward, _ = ctx
    out = []
    for c in commands:
        par = c.get("parameters") if isinstance(c, dict) else None
        if (isinstance(c, dict) and c.get("commandType") == "MOVE_TO" and isinstance(par, dict)
                and _finite(par.get("target_x")) and (par["target_x"] - me[0]) * toward < -BACKWARD_TOLERANCE):
            c = {**c, "parameters": {**par, "target_x": round(me[0] + toward * NO_BACK_STEP, 1),
                                     "target_y": round(me[1] * 0.7, 1), "sprint": False}}
        out.append(c)
    return out


def gk_leash(commands, game_state: dict, team_id: int, player_id: int):
    """GK only: a MOVE_TO target outside our penalty area is pulled back to its edge (design units)."""
    if player_id != GK_ID or not isinstance(commands, list):
        return commands
    my_goal_x, _ = get_goal_positions(team_id)
    inward = 1.0 if my_goal_x < 0 else -1.0
    lo, hi = sorted((my_goal_x, my_goal_x + inward * GK_MAX_DEPTH))
    out = []
    for c in commands:
        par = c.get("parameters") if isinstance(c, dict) else None
        if isinstance(c, dict) and c.get("commandType") == "MOVE_TO" and isinstance(par, dict):
            par = dict(par)
            if _finite(par.get("target_x")):
                par["target_x"] = max(lo, min(hi, par["target_x"]))
            if _finite(par.get("target_y")):
                par["target_y"] = max(-GK_MAX_WIDTH, min(GK_MAX_WIDTH, par["target_y"]))
            c = {**c, "parameters": par}
        out.append(c)
    return out
