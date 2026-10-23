"""Explicit, team-aware possession resolution.

AFC PORT of lab/possession_lab/possession.py (verbatim except imports and this note). See AFC_RUNBOOK.md.

Upstream (lib/state.py) reduces the holder to a bare number ("agentId_4" -> 4) and then
guesses the team from whichever player with that number comes first in players[]. Both
teams use agentId_0..4 in the sample state, so that guess is wrong half the time.

This module resolves possession into one of four states, relative to the asking team:

    OURS     a player on our team holds the ball
    THEIRS   an opponent holds the ball
    LOOSE    the state reports no holder (and does not contradict itself)
    UNKNOWN  the state does not let us say who holds it

Resolution order — the first rule that applies wins:

  0. No holder reported:
       isFree is True or absent       -> LOOSE
       isFree is False                -> UNKNOWN ("not free, but no holder": in flight? dead ball?)
     Holder reported but isFree is True -> UNKNOWN (self-contradictory)
  1. Match the holder id EXACTLY against each player's own id (agentId string, or playerId
     int in the old format). Never by extracted number. No match -> UNKNOWN.
  2. Authoritative team field, if the server sends one (see TEAM_FIELD_CANDIDATES — these
     names are PLACEHOLDERS, none exists in the sample). Picks the matching candidate;
     invalid value or no candidate on that team -> UNKNOWN.
  3. Exactly one player carries that id -> that player (globally unique ids, or the other
     team's same-number player is absent from players[]).
  4. Several players carry the id (the sample's per-team numbering): conservative proximity
     inference BETWEEN THOSE CANDIDATES ONLY — never among all players. Accept the nearest
     candidate only if it is within CONTROL_RADIUS of the ball AND every other candidate is
     at least MIN_SEPARATION away. Otherwise UNKNOWN.

A candidate without a known team, or (for rule 4) without a position, makes the result
UNKNOWN rather than guessed.
"""

import math
from dataclasses import dataclass
from enum import Enum

# Rule 4 thresholds, in pitch units (assumed metres: pitch x is -55..55). In the upstream
# sample the true holder is 1.3 m from the ball and the same-number opponent 16.2 m.
# Both are ASSUMPTIONS to calibrate against real server states.
CONTROL_RADIUS = 3.0
MIN_SEPARATION = 5.0

# Hypothetical authoritative team fields. None of these appear in the sample state or in
# any upstream code; they are the single place to put the real name once it is known.
TEAM_FIELD_CANDIDATES = ("possessionTeamCode", "possessionTeamId", "possessionTeam")


class Possession(str, Enum):
    OURS = "OURS"
    THEIRS = "THEIRS"
    LOOSE = "LOOSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PossessionInfo:
    state: Possession
    holder_idx: int | None = None   # holder's number (0-4) when the id parses, even if UNKNOWN
    holder_team: int | None = None  # 0 HOME / 1 AWAY, only when resolved
    source: str = ""                # which rule decided (see module docstring)
    detail: str = ""

    def is_holder(self, player_idx: int) -> bool:
        """True only when OUR player `player_idx` holds the ball — team AND number match."""
        return self.state is Possession.OURS and self.holder_idx == player_idx

    def might_be_holder(self, player_idx: int) -> bool:
        """UNKNOWN, but the reported number is ours: we cannot rule out holding the ball."""
        return self.state is Possession.UNKNOWN and self.holder_idx == player_idx


# --- schema helpers (accept the new and old server formats) -------------------------------

def player_key(p: dict):
    """The player's own id, compared exactly: agentId (new) or playerId (old)."""
    return p["agentId"] if "agentId" in p else p.get("playerId")


def player_number(p: dict) -> int | None:
    """0-4 number, or None if it cannot be read (upstream returns 0 = the GK instead)."""
    if "agentId" in p:
        try:
            return int(str(p["agentId"]).rsplit("_", 1)[-1])
        except ValueError:
            return None
    pid = p.get("playerId")
    return pid if type(pid) is int else None


def player_team(p: dict) -> int | None:
    """0 HOME / 1 AWAY from teamCode (new) or teamId (old); None if absent or invalid."""
    code = p.get("teamCode")
    if code in ("home", "away"):
        return 0 if code == "home" else 1
    tid = p.get("teamId")
    return tid if type(tid) is int and tid in (0, 1) else None


def _holder_key(ball: dict):
    for field in ("possessionAgentId", "possessionPlayerId"):
        if ball.get(field) is not None:
            return ball[field]
    return None


def _team_hint(ball: dict):
    """(team, field) from an authoritative team field; team is 'invalid' if unreadable."""
    for field in TEAM_FIELD_CANDIDATES:
        value = ball.get(field)
        if value is None:
            continue
        if value in ("home", "away", "HOME", "AWAY"):
            return (0 if value.lower() == "home" else 1), field
        if type(value) is int and value in (0, 1):
            return value, field
        return "invalid", field
    return None, None


def _pos(obj: dict):
    pos = obj.get("position")
    if isinstance(pos, dict) and isinstance(pos.get("x"), (int, float)) and isinstance(pos.get("y"), (int, float)):
        return pos["x"], pos["y"]
    return None


# --- resolution -----------------------------------------------------------------------------

def resolve_possession(ball: dict, players: list, team_id: int) -> PossessionInfo:
    """Resolve who holds the ball, relative to `team_id`. Pure and deterministic."""
    U = Possession.UNKNOWN
    key = _holder_key(ball)
    is_free = ball.get("isFree")

    # Rule 0: no holder / contradictions
    if key is None:
        if is_free is False:
            return PossessionInfo(U, source="not_free_no_holder",
                                  detail="isFree=false but no holder id")
        return PossessionInfo(Possession.LOOSE, source="no_holder")

    number = player_number({"agentId": key} if isinstance(key, str) else {"playerId": key})
    if is_free is True:
        return PossessionInfo(U, number, source="contradictory",
                              detail=f"holder {key!r} but isFree=true")

    # Rule 1: exact id match
    candidates = [p for p in players if player_key(p) == key]
    if not candidates:
        return PossessionInfo(U, number, source="unmatched_id",
                              detail=f"no player has id {key!r}")
    if any(player_team(c) is None for c in candidates):
        return PossessionInfo(U, number, source="candidate_without_team")

    def resolved(p, source, detail=""):
        team, num = player_team(p), player_number(p)
        if num is None:
            return PossessionInfo(U, None, source="unreadable_holder_number")
        state = Possession.OURS if team == team_id else Possession.THEIRS
        return PossessionInfo(state, num, team, source, detail)

    # Rule 2: authoritative team field (hypothetical names)
    hint, field = _team_hint(ball)
    if field is not None:
        if hint == "invalid":
            return PossessionInfo(U, number, source="invalid_team_field",
                                  detail=f"{field}={ball[field]!r}")
        on_team = [c for c in candidates if player_team(c) == hint]
        if len(on_team) == 1:
            return resolved(on_team[0], "team_field", f"{field}={ball[field]!r}")
        return PossessionInfo(U, number, source="team_field_mismatch",
                              detail=f"{field}={ball[field]!r} matches {len(on_team)} candidates")

    # Rule 3: id is unique among listed players
    if len(candidates) == 1:
        return resolved(candidates[0], "unique_id")

    # Rule 4: conservative proximity inference between same-id candidates only
    ball_xy = _pos(ball)
    cand_xy = [_pos(c) for c in candidates]
    if ball_xy is None or None in cand_xy:
        return PossessionInfo(U, number, source="ambiguous_no_positions")
    ranked = sorted((math.dist(xy, ball_xy), i) for i, xy in enumerate(cand_xy))
    (d0, i0), rivals = ranked[0], ranked[1:]
    distances = ", ".join(f"{d:.1f}" for d, _ in ranked)
    if d0 <= CONTROL_RADIUS and all(d >= MIN_SEPARATION for d, _ in rivals):
        return resolved(candidates[i0], "inferred_proximity", f"candidate distances {distances}")
    return PossessionInfo(U, number, source="ambiguous",
                          detail=f"{len(candidates)} players with id {key!r}; distances {distances}")


def describe(info: PossessionInfo) -> str:
    """Text for the 'Ball: ... held by <X>' summary line. OURS/THEIRS/LOOSE match upstream."""
    if info.state is Possession.OURS:
        return f"MY player {info.holder_idx}"
    if info.state is Possession.THEIRS:
        return f"OPP player {info.holder_idx}"
    if info.state is Possession.LOOSE:
        return "free"
    if info.holder_idx is not None:
        return f"UNKNOWN (a player #{info.holder_idx}, team unclear)"
    return "UNKNOWN"
