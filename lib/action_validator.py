"""Deterministic action validator: accept a model reply as-is, or reject it.

AFC PORT of lab/validator_lab/validator.py (verbatim except imports, this note and the lines
marked `# AFC`: PRESS_BALL and INTERCEPT are rejected on our own ball). See AFC_RUNBOOK.md.

Where it sits. Upstream parse_commands() (lib/parsing.py) does three things at once: it
finds the JSON, it REPAIRS commands (overwrites playerId/teamId, clamps MOVE_TO, invents a
target_player_id) and it drops unknown types. After it has run, a wrong player id or an
off-pitch target can no longer be seen. So the lab pipeline is:

    reply text --extract_reply()--> raw JSON value (same extraction as parse_commands:
                                    same greedy regex, same json_tolerant recovery)
               --validate_reply()--> Verdict: ok + the command, or rejected + issues

The validator never edits a command into a different one. An accepted command leaves with
exactly the parameters the model wrote; the only change is that playerId/teamId are filled
in (they must already match when present). A rejected reply is handed to the fallback by
the handler, which is upstream's own layer-2 policy, and never "fixed".

Checks, in this order (every applicable issue is reported, in a stable order):
  reply    JSON found; a list (a bare object counts as one command, as upstream allows);
           not empty; exactly COMMANDS_PER_REPLY commands
  command  an object; only COMMAND_KEYS; commandType known; playerId/teamId (if present)
           are ints equal to the controlled player / team; duration (if present) int >= 0
  params   an object; no unknown keys; required ones present; types, enums, ranges,
           pitch bounds; player targets 0-4, not myself, listed on the pitch when the
           roster for that team is known
  role     GK-only commands from the GK
  ball     possession preconditions via possession_lab (OURS-and-me / THEIRS / not OURS)
"""

import math
import re
from dataclasses import dataclass

from parsing import _loads  # upstream: strict parse, then json_tolerant recovery

from possession import (Possession, PossessionInfo, describe, player_number,
                                       player_team, resolve_possession)

from action_schema import (COMMAND_KEYS, COMMANDS_PER_REPLY, GK_ID, PLAYER_IDS, SCHEMAS, TEAM_CODES)

_ARRAY = re.compile(r"\[[\s\S]*\]")  # the same greedy pattern as lib/parsing.parse_commands


@dataclass(frozen=True)
class Issue:
    code: str
    field: str
    detail: str = ""

    def __str__(self):
        return f"{self.code}({self.field})" + (f": {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str          # "ok" or the reply-level code
    commands: tuple = ()  # the accepted command(s), ids filled in; empty when rejected
    issues: tuple = ()


@dataclass(frozen=True)
class Context:
    team_id: int
    player_id: int
    possession: PossessionInfo
    on_pitch: dict       # {0: frozenset of numbers listed for HOME, 1: ... for AWAY}


def context_from_state(game_state: dict, team_id: int, player_id: int) -> Context:
    ball = game_state.get("ball", {})
    players = game_state.get("players", [])
    on_pitch = {0: set(), 1: set()}
    for p in players:
        team, number = player_team(p), player_number(p)
        if team is not None and number is not None:
            on_pitch[team].add(number)
    return Context(team_id, player_id, resolve_possession(ball, players, team_id),
                   {t: frozenset(n) for t, n in on_pitch.items()})


# --- extraction -----------------------------------------------------------------------------

def extract_reply(text: str, on_recovered=None):
    """The raw JSON value parse_commands would have tagged: a list, a dict, or None."""
    match = _ARRAY.search(text)
    if match:
        value = _loads(match.group(), on_recovered)
        if isinstance(value, list):
            return value
    value = _loads(text, on_recovered)
    return value if isinstance(value, (list, dict)) else None


# --- reply level ----------------------------------------------------------------------------

def validate_reply(value, ctx: Context) -> Verdict:
    if value is None:
        return Verdict(False, "no_json", issues=(Issue("no_json", "reply", "no JSON array found"),))
    if isinstance(value, dict):
        value = [value]  # [C] parse_commands accepts a single bare command object
    if not isinstance(value, list):
        return Verdict(False, "not_a_list", issues=(Issue("not_a_list", "reply", type(value).__name__),))
    if not value:
        return Verdict(False, "empty_list", issues=(Issue("empty_list", "reply"),))

    count = _count_issues(len(value))
    issues = []
    for i, raw in enumerate(value):
        prefix = f"[{i}]." if len(value) > 1 else ""
        issues += [Issue(x.code, prefix + x.field, x.detail) for x in validate_command(raw, ctx)]
    if count:
        return Verdict(False, "multiple_commands", issues=tuple(count + issues))
    if issues:
        return Verdict(False, "invalid_command", issues=tuple(issues))
    return Verdict(True, "ok", tuple(_tag(raw, ctx) for raw in value))


def _count_issues(n: int) -> list:
    if n != COMMANDS_PER_REPLY:
        return [Issue("multiple_commands", "reply", f"{n} commands; the contract is exactly {COMMANDS_PER_REPLY}")]
    return []


def _tag(raw: dict, ctx: Context) -> dict:
    out = {**raw, "teamId": ctx.team_id, "playerId": ctx.player_id}
    if isinstance(raw.get("parameters"), dict):
        out["parameters"] = dict(raw["parameters"])
    return out


# --- command level --------------------------------------------------------------------------

def validate_command(raw, ctx: Context) -> list:
    if not isinstance(raw, dict):
        return [Issue("not_an_object", "command", f"{type(raw).__name__} {raw!r:.40}")]
    issues = [Issue("unknown_key", key) for key in raw if key not in COMMAND_KEYS]

    ctype, spec = raw.get("commandType"), None
    if "commandType" not in raw:
        issues.append(Issue("missing_command_type", "commandType"))
    elif not isinstance(ctype, str) or ctype not in SCHEMAS:
        issues.append(Issue("unknown_command", "commandType", repr(ctype)))
    else:
        spec = SCHEMAS[ctype]

    issues += _check_identity(raw, ctx)
    issues += _check_duration(raw)

    params = raw.get("parameters", {})
    if not isinstance(params, dict):
        issues.append(Issue("bad_parameters", "parameters", f"expected an object, got {type(params).__name__}"))
    elif spec is not None:
        issues += _check_params(spec, params, ctx)

    if spec is not None:
        if spec.gk_only and ctx.player_id != GK_ID:
            issues.append(Issue("not_goalkeeper", "commandType", f"{ctype} is GK only; I am #{ctx.player_id}"))
        issues += _check_possession(ctype, spec, ctx)
    return issues


def _check_identity(raw: dict, ctx: Context) -> list:
    issues = []
    for key, expected, code in (("playerId", ctx.player_id, "wrong_player"),
                                ("teamId", ctx.team_id, "wrong_team")):
        if key not in raw:
            continue
        value = raw[key]
        if type(value) is not int:
            issues.append(Issue("bad_type", key, f"expected int, got {value!r}"))
        elif value != expected:
            issues.append(Issue(code, key, f"{value} but this agent controls {key}={expected}"))
    return issues


def _check_duration(raw: dict) -> list:
    if "duration" not in raw:
        return []
    value = raw["duration"]
    if type(value) is not int:
        return [Issue("bad_type", "duration", f"expected int, got {value!r}")]
    if value < 0:
        return [Issue("out_of_range", "duration", f"{value} < 0")]
    return []


def _check_params(spec, params: dict, ctx: Context) -> list:
    known = {p.name: p for p in spec.params}
    issues = [Issue("unknown_parameter", f"parameters.{key}") for key in params if key not in known]
    for p in spec.params:
        if p.name not in params:
            if p.required:
                issues.append(Issue("missing_parameter", f"parameters.{p.name}"))
            continue
        issues += _check_value(p, params[p.name], params, ctx)
    return issues


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _range_issue(p, value):
    if (p.lo is not None and value < p.lo) or (p.hi is not None and value > p.hi):
        code = "out_of_bounds" if p.coordinate else "out_of_range"
        lo = "-inf" if p.lo is None else p.lo
        hi = "inf" if p.hi is None else p.hi
        return Issue(code, f"parameters.{p.name}", f"{value} not in [{lo}, {hi}]")
    return None


def _check_value(p, value, params: dict, ctx: Context) -> list:
    field = f"parameters.{p.name}"
    if p.kind == "number":
        if not _is_number(value) or not math.isfinite(value):
            return [Issue("bad_type", field, f"expected a finite number, got {value!r}")]
        issue = _range_issue(p, value)
        return [issue] if issue else []
    if p.kind == "bool":
        return [] if type(value) is bool else [Issue("bad_type", field, f"expected bool, got {value!r}")]
    if p.kind == "choice":
        if not any(type(value) is type(c) and value == c for c in p.choices):
            return [Issue("bad_enum", field, f"{value!r} not one of {list(p.choices)}")]
        return []
    if p.kind == "player":
        return _check_target(value, params, ctx, field, p.team)
    raise ValueError(f"unknown parameter kind {p.kind!r}")


def _check_target(value, params: dict, ctx: Context, field: str, team_rule: str) -> list:
    if type(value) is not int:
        return [Issue("bad_type", field, f"expected int player id, got {value!r}")]
    if value not in PLAYER_IDS:
        return [Issue("bad_target", field, f"{value} not in {list(PLAYER_IDS)}")]
    if team_rule == "mine":
        team = ctx.team_id
    elif team_rule == "theirs":
        team = 1 - ctx.team_id
    else:
        team = TEAM_CODES.get(params.get(team_rule))  # bad target_team is reported on its own
        if team is None:
            return []
    if team == ctx.team_id and value == ctx.player_id:
        return [Issue("target_is_self", field, f"#{value} is me")]
    listed = ctx.on_pitch.get(team)
    if listed and value not in listed:
        side = "my team" if team == ctx.team_id else "the opponents"
        return [Issue("target_not_on_pitch", field, f"#{value} is not listed for {side}")]
    return []


def _check_possession(ctype: str, spec, ctx: Context) -> list:
    info = ctx.possession
    if spec.needs == "my_ball" and not info.is_holder(ctx.player_id):
        return [Issue("needs_ball", "possession",
                      f"{ctype} needs me (#{ctx.player_id}) on the ball; {_possession_text(info)}")]
    if spec.needs == "their_ball" and info.state is not Possession.THEIRS:
        return [Issue("needs_opponent_ball", "possession",
                      f"{ctype} needs an opponent on the ball; {_possession_text(info)}")]
    if spec.needs == "not_our_ball" and info.state is Possession.OURS:  # AFC
        return [Issue("own_ball", "possession",
                      f"{ctype} targets our own ball; {_possession_text(info)}")]
    return []


def _possession_text(info: PossessionInfo) -> str:
    return f"possession is {info.state.value} ({describe(info)}, via {info.source})"
