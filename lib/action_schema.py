"""The command contract the validator enforces, with where every rule comes from.

AFC PORT of lab/validator_lab/schema.py (verbatim except imports, this note and the lines
marked `# AFC`: PRESS_BALL and INTERCEPT need="not_our_ball"). See AFC_RUNBOOK.md.

Provenance tags used below:
  [P] stated in the upstream SYSTEM_PROMPTs (the "Available Commands" block is identical in
      all 20 prompts that spell out parameters; all 25 say "exactly ONE command")
  [R] ai-team-strands-*/README.md ("Available Commands", "Player IDs and Positions")
  [C] upstream code: lib/parsing.py (VALID_COMMANDS, MOVE_TO clamp), lib/state.py (goal x)
  [L] LAB POLICY: chosen here, stated nowhere upstream; change it freely

Nothing here is verified against the game server. [P]/[R]/[C] are what the upstream CLIENT
tells the model and assumes; the server may accept more (other methods, several commands,
missing booleans) or less.
"""

from dataclasses import dataclass

# [C] lib/parsing.py clamps MOVE_TO to x in [-55, 55], y in [-35, 35]; lib/state.py puts the
# goals at x = -55 / +55. [P] "x roughly -55 to +55, y roughly -35 to +35" (gateway prompts:
# "x=-55 to +55, y=-35 to +35"). Inclusive. [L] outside is REJECTED, not clamped.
FIELD_X = 55.0
FIELD_Y = 35.0

# [R] player table 0..4, [P] "5v5". [R]/[P] player 0 is the goalkeeper.
PLAYER_IDS = (0, 1, 2, 3, 4)
GK_ID = 0

# [P] the example command carries commandType, playerId, parameters, duration;
# [C] every fallback command also carries teamId. [L] any other top-level key is rejected.
COMMAND_KEYS = ("commandType", "playerId", "teamId", "parameters", "duration")

# [P] "Return ONLY a JSON array with exactly ONE command" (all 25 prompts). [C] every
# fallback and last-resort command list has length 1. Server limit: unverified.
COMMANDS_PER_REPLY = 1

TEAM_CODES = {"HOME": 0, "AWAY": 1}  # [P] FOLLOW_PLAYER target_team; [P] "Team 0 (HOME)"


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                  # "number" | "bool" | "choice" | "player"
    required: bool
    choices: tuple = ()        # kind "choice": allowed values, compared with exact type
    lo: float | None = None    # kind "number": inclusive range
    hi: float | None = None
    coordinate: bool = False   # out of range -> "out_of_bounds" (pitch) not "out_of_range"
    team: str = ""             # kind "player": "mine" | "theirs" | "target_team" (other param)


@dataclass(frozen=True)
class CommandSpec:
    params: tuple
    needs: str = ""            # "" | "my_ball" (I hold it) | "their_ball" (opponent holds it)
    #                            | "not_our_ball" (AFC: anything but a teammate or me holding it)
    gk_only: bool = False


# [L] required = every NON-boolean parameter the prompt lists (they select WHAT the action
# is: a point, a target, a method). Booleans (sprint, aggressive) are modifiers: optional,
# type-checked when present, never filled in. [L] distance >= 0 (the prompt only says float).
def _num(name, lo=None, hi=None, coordinate=False):
    return Param(name, "number", True, lo=lo, hi=hi, coordinate=coordinate)


def _flag(name):
    return Param(name, "bool", False)


def _choice(name, *choices):
    return Param(name, "choice", True, choices=choices)


def _player(team):
    return Param("target_player_id", "player", True, team=team)


SCHEMAS = {
    # --- one-shot [P][R] ---
    "MOVE_TO": CommandSpec((_num("target_x", -FIELD_X, FIELD_X, coordinate=True),
                            _num("target_y", -FIELD_Y, FIELD_Y, coordinate=True),
                            _flag("sprint"))),
    # [P] PASS/SHOOT "only if you have ball"
    "PASS": CommandSpec((_player("mine"), _choice("type", "GROUND", "AERIAL", "THROUGH")),
                        needs="my_ball"),
    "SHOOT": CommandSpec((_choice("aim_location", "TL", "TR", "BL", "BR", "CENTER"),
                          _num("power", 0.0, 1.0)),
                         needs="my_ball"),
    # [L] needs_their_ball: the prompt only says "risky aggressive tackle"
    "SLIDE_TACKLE": CommandSpec((_player("theirs"), _flag("sprint"), _num("distance", 0.0)),
                                needs="their_ball"),
    # [P] "GK only" (outfield prompts); [P] GK priority 1 "If you have the ball -> GK_DISTRIBUTE".
    # method is THROW|KICK only: PUNT appears nowhere upstream.
    "GK_DISTRIBUTE": CommandSpec((_player("mine"), _choice("method", "THROW", "KICK")),
                                 needs="my_ball", gk_only=True),
    # --- maintained [P][R] ---
    # AFC: not on our own ball. Deployed team run 2026-10-18: with our GK holding the ball, DEF
    # and MID both answered PRESS_BALL. The fallback already presses only THEIRS (or LOOSE).
    "PRESS_BALL": CommandSpec((_num("intensity", 0.0, 1.0),), needs="not_our_ball"),
    "MARK": CommandSpec((_player("theirs"), _choice("tightness", "LOOSE", "TIGHT"))),
    # AFC: same reasoning as PRESS_BALL: there is nothing to intercept when we hold the ball.
    "INTERCEPT": CommandSpec((_flag("aggressive"),), needs="not_our_ball"),
    "FOLLOW_PLAYER": CommandSpec((_player("target_team"), _choice("target_team", "HOME", "AWAY"),
                                  _num("distance", 0.0))),
    # --- tactical [P][R] ---
    "SET_STANCE": CommandSpec((_choice("stance", 0, 1, 2),)),
    "CLEAR_OVERRIDE": CommandSpec(()),
    "RESET": CommandSpec(()),
}
