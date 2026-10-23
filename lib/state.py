"""Game state summarization utilities for AI soccer agents.

AFC PORT of lab/experiment_5/state.py (upstream lib/state.py + team-aware possession `# LAB:`,
lab Exp 2a, + precise stamina `# LAB-5:`, Exp 5), plus the safe gameTime `# AFC:` (lab Exp 8).
"""

import math
import re

# AFC (2026-10-23): a tiny negative coordinate renders as "-0.0"; the same spot seen from the other side
# renders "0.0". Normalised so a team-1 home view gives the model exactly the team-0 text (test_symmetry).
_NEG_ZERO = re.compile(r"(?<![\d.])-(0\.0+)(?![\d])")

from possession import Possession, describe, resolve_possession  # LAB

from stamina import format_stamina  # LAB-5


# ---------------------------------------------------------------------------
# Format-agnostic helpers — handle both new (agentId/teamCode/possessionAgentId)
# and old (playerId/teamId/possessionPlayerId) game server formats.
# ---------------------------------------------------------------------------

def _player_idx(p: dict) -> int:
    """Numeric index (0-4) from a player dict — new agentId or old playerId."""
    if "agentId" in p:
        try:
            return int(p["agentId"].rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return 0
    return p.get("playerId", 0)


def _is_my_team(p: dict, team_id: int) -> bool:
    """True if player belongs to team_id — new teamCode or old teamId."""
    if "teamCode" in p:
        return p["teamCode"] == ("home" if team_id == 0 else "away")
    return p.get("teamId") == team_id


def _possession_idx(ball: dict):
    """Numeric possession player index from ball dict — new possessionAgentId or old possessionPlayerId.
    Returns int or None."""
    agent_id = ball.get("possessionAgentId")
    if agent_id is not None:
        try:
            return int(agent_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return None
    return ball.get("possessionPlayerId")


def get_goal_positions(team_id: int) -> tuple[float, float]:
    """Return (my_goal_x, opp_goal_x) based on team."""
    if team_id == 0:
        return -55.0, 55.0
    return 55.0, -55.0


def get_possession_info(ball: dict, players: list, team_id: int) -> tuple:
    """Return (possession_id, ball_status_str, we_have_ball)."""
    # LAB: same signature, but team-aware. Upstream took the FIRST player in players[]
    # whose number matched, so the answer depended on list order. possession_id is now
    # the holder's number only when the holder is resolved (OURS/THEIRS), else None.
    info = resolve_possession(ball, players, team_id)
    resolved = info.state in (Possession.OURS, Possession.THEIRS)
    return (info.holder_idx if resolved else None), describe(info), info.state is Possession.OURS


def dist(pos1: dict, pos2: dict) -> float:
    """Euclidean distance between two position dicts with x,y keys."""
    return math.sqrt(
        (pos1.get("x", 0) - pos2.get("x", 0)) ** 2
        + (pos1.get("y", 0) - pos2.get("y", 0)) ** 2
    )


def _format_game_time(value) -> str:
    """AFC: a finite number renders as before ("120.5" -> "120s"); anything else "unknown".

    Upstream `game_state.get("gameTime", 0)` + `:.0f` raised for None/strings/lists (the
    tick then went to the fallback without calling the model), showed True as "1s", NaN
    as "nans", and a missing value as an invented "0s". See lab/experiment_8.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "unknown"
    return f"{value:.0f}s"


def _real_xy(pos) -> tuple | None:
    """(x, y) when both are real finite numbers, else None (never a guessed 0)."""
    if not isinstance(pos, dict):
        return None
    x, y = pos.get("x"), pos.get("y")
    if all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
        return (x, y)
    return None


def _teammate_detail(pos, opponents, opp_goal_x, with_goal=True) -> str:
    """AFC: ' distOppGoal=… nearestOpp=…' for one teammate (experiment, off by default)."""
    me = _real_xy(pos)
    opp = [xy for xy in (_real_xy(o.get("position")) for o in opponents) if xy]
    goal = f"{abs(me[0] - opp_goal_x):.1f}" if me else "unknown"
    near = f"{min(math.hypot(me[0] - x, me[1] - y) for x, y in opp):.1f}" if me and opp else "unknown"
    return (f" distOppGoal={goal}" if with_goal else "") + f" nearestOpp={near}"


def summarize_state(
    game_state: dict,
    team_id: int,
    my_player_id: int,
    position_label: str,
    teammate_detail: bool = False,
) -> str:
    """Build a concise text summary of the game state for a single-player agent.

    AFC: teammate_detail=True (default False, experiment only) appends to every teammate line
    distOppGoal (the same |x - opponent goal x| as the YOUR PLAYER line) and nearestOpp (euclidean
    distance to the closest opponent, in pitch units, as distBall). Either is "unknown" when it
    cannot be computed from real numbers (the YOUR PLAYER line, unchanged, still reads a missing x
    as 0). nearestOpp is NOT "free": it ignores passing lanes and who marks whom, and the opponent
    goalkeeper counts as an opponent.
    """
    ball = game_state.get("ball", {})
    ball_pos = ball.get("position", {"x": 0, "y": 0})
    score = game_state.get("score", {})
    game_time = _format_game_time(game_state.get("gameTime"))  # AFC: was .get("gameTime", 0), shown as :.0f
    play_mode = game_state.get("playMode", 0)
    players = game_state.get("players", [])

    my_team = sorted(
        [p for p in players if _is_my_team(p, team_id)],
        key=lambda p: _player_idx(p),
    )
    opponents = sorted(
        [p for p in players if not _is_my_team(p, team_id)],
        key=lambda p: _player_idx(p),
    )

    me = next((p for p in my_team if _player_idx(p) == my_player_id), None)
    info = resolve_possession(ball, players, team_id)  # LAB: was get_possession_info()
    ball_status = describe(info)

    my_goal_x, opp_goal_x = get_goal_positions(team_id)

    lines = [
        f"Time: {game_time} | Score: {score.get('home',0)}-{score.get('away',0)} | "
        f"Team: {team_id} ({'HOME' if team_id == 0 else 'AWAY'}) | PlayMode: {play_mode}",
        f"Ball: ({ball_pos.get('x',0):.1f}, {ball_pos.get('y',0):.1f}) held by {ball_status}",
        f"Your goal at x={my_goal_x:.0f} | Opponent goal at x={opp_goal_x:.0f}",
        "",
    ]

    # My player info
    if me:
        pos = me.get("position", {})
        stam = format_stamina(me.get("stamina"))  # LAB-5: was me.get("stamina", 100), shown as :.0f
        dist_ball = dist(pos, ball_pos)
        # LAB: upstream compared numbers only (`possession_id == my_player_id`), so the
        # opponent's #0 holding the ball told our GK hasBall=True.
        has_ball = "unknown" if info.might_be_holder(my_player_id) else info.is_holder(my_player_id)
        extra = f" distOppGoal={abs(pos.get('x', 0) - opp_goal_x):.1f}" if position_label in ("MID", "FWD1", "FWD2") else ""
        lines.append(
            f">>> YOUR PLAYER ({position_label}, id={my_player_id}): "
            f"pos=({pos.get('x',0):.1f},{pos.get('y',0):.1f}) "
            f"stam={stam} distBall={dist_ball:.1f}{extra} hasBall={has_ball}"
        )
    lines.append("")

    # Teammates
    lines.append("Teammates:")
    for p in my_team:
        if _player_idx(p) == my_player_id:
            continue
        pos = p.get("position", {})
        pid = _player_idx(p)
        role = "GK" if pid == 0 else f"P{pid}"
        extra = ""
        if position_label == "MID":
            extra = f" distOppGoal={abs(pos.get('x', 0) - opp_goal_x):.1f}"
        if teammate_detail:  # AFC: experiment, see the docstring (MID already shows distOppGoal)
            extra += _teammate_detail(pos, opponents, opp_goal_x, with_goal=position_label != "MID")
        lines.append(f"  {role}(id={pid}): ({pos.get('x',0):.1f},{pos.get('y',0):.1f}){extra}")

    lines.append("")

    # Opponents
    opp_header = "Opponents (defenders to watch):" if position_label in ("FWD1", "FWD2") else "Opponents:"
    lines.append(opp_header)
    for p in opponents:
        pos = p.get("position", {})
        pid = _player_idx(p)
        d_goal = abs(pos.get("x", 0) - my_goal_x)
        d_me = dist(pos, me.get("position", {})) if me else 0
        lines.append(f"  P{pid}: ({pos.get('x',0):.1f},{pos.get('y',0):.1f}) distToMyGoal={d_goal:.1f} distToMe={d_me:.1f}")

    return _NEG_ZERO.sub(r"\1", "\n".join(lines))
