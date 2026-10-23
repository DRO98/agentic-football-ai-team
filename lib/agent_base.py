"""Base agent factory for AI soccer position agents.

AFC changes (tournament copy), each marked `# AFC:`:
  - explicit conversation history: SlidingWindowConversationManager(window_size=AFC_HISTORY_WINDOW, default 0)
  - layer 1 validates the raw model reply (action_validator) instead of letting parse_commands repair it
  - optional LAB_CAPTURE log line (AFC_CAPTURE_STATE)
  - optional deterministic pre-model rule (rule_fn, lib/rules.py), validated before it is sent
  - "AFC_PROTOCOL" warning (key names only) when the payload lacks gameState/teamId/myPlayers
"""

import json
import os
from typing import Callable
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel

from state import summarize_state
from fallback import FallbackConfig, build_last_resort
from action_validator import context_from_state, extract_reply, validate_reply  # AFC
from frame import from_home_view, to_design_frame, to_home_view, to_server_frame  # AFC: server pitch <-> design pitch; team 1 plays a home view
from rules import gk_leash, limit_sprint, no_backward_carry  # AFC: no sprint on low stamina; the GK stays in its area


# AFC: optional game-state capture, OFF unless the env var AFC_CAPTURE_STATE is 1/true/yes/on.
# Each invocation then logs one line "LAB_CAPTURE {json}" (the payload's prompt data) that
# lab/experiment_7 (run.py extract / inspect) reads back. Read per call, so a runtime can be
# switched by its envVars in agentcore/agentcore.json (needs a redeploy) or locally via $env:.
CAPTURE_ENV = "AFC_CAPTURE_STATE"
CAPTURE_MARKER = "LAB_CAPTURE "
PROTOCOL_MARKER = "AFC_PROTOCOL "  # AFC: logged when the payload does not have the expected shape


def capture_enabled() -> bool:
    return os.environ.get(CAPTURE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# AFC: conversation history. Upstream `Agent(model, system_prompt)` gets Strands' default
# SlidingWindowConversationManager(window_size=40): each tick resends up to 20 earlier game
# states and replies, and the module-level agent keeps them across matches (lab Exp 4).
# 0 = stateless: Strands removes all non-pinned messages after every call, so each tick sends
# only the system prompt + the current summary. AFC_HISTORY_WINDOW (an int >= 0) overrides it
# when the agent is created; a missing, non-integer or negative value means the default, 0.
# NullConversationManager is NOT used: it keeps the whole history (it never trims).
HISTORY_ENV = "AFC_HISTORY_WINDOW"
DEFAULT_HISTORY_WINDOW = 0


def history_window() -> int:
    raw = os.environ.get(HISTORY_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HISTORY_WINDOW
    return value if value >= 0 else DEFAULT_HISTORY_WINDOW


def create_agent(system_prompt: str, model_id: str = "us.amazon.nova-micro-v1:0", model=None) -> Agent:
    """Create a Strands Agent with the given system prompt.

    AFC: `model` lets tests pass an offline model; normally a BedrockModel(model_id) is built.
    """
    model = model if model is not None else BedrockModel(model_id=model_id)
    return Agent(model=model, system_prompt=system_prompt,
                 conversation_manager=SlidingWindowConversationManager(window_size=history_window()))


def _issues_text(verdict) -> str:
    return f"{verdict.reason}: " + "; ".join(str(i) for i in verdict.issues)


def create_invoke_handler(
    app,
    agent: Agent,
    my_player_id: int,
    position_label: str,
    fallback_fn: Callable[[dict, int, int], list[dict]],
    fallback_cfg: FallbackConfig,
    rule_fn: Callable[[dict, int, int], list[dict] | None] | None = None,
):
    """Create and register the @app.entrypoint invoke handler.

    AFC: before layer 1, `rule_fn(game_state, team_id, player_id)` (lib/rules.py) may answer the
    tick deterministically; its command must pass the validator, else the tick goes on to layer 1.

    Three layers of error handling, from best to worst:
      1. LLM response → validated as written (AFC: action_validator; rejected -> layer 2)
      2. fallback_fn(game_state, team_id, my_player_id) → rule-based commands (AFC: validated too)
      3. last-resort command from fallback_cfg → single safe command
    """
    log = app.logger
    last_resort = build_last_resort(fallback_cfg, my_player_id)

    @app.entrypoint
    async def invoke(payload, context):
        try:
            prompt = payload.get("prompt", "{}")
            prompt_data = json.loads(prompt) if isinstance(prompt, str) else prompt

            if capture_enabled():  # AFC: before the summary, so a state that breaks it is still captured
                log.info(CAPTURE_MARKER + json.dumps(prompt_data, separators=(",", ":"), default=str))

            game_state = prompt_data.get("gameState", {})
            team_id = prompt_data.get("teamId", 0)
            # AFC: the server's pitch is ~8.6x smaller, with the ball's lateral in z (lib/frame.py).
            # Everything below runs in the design frame; send() converts the reply back.
            game_state, scale = to_design_frame(game_state)
            # AFC: team 1 is rotated into a home view (the models assume "forward = +x"); real_team is kept
            # for logging and outgoing commands, team_id is the view everything below runs in.
            real_team = team_id
            game_state, flipped = to_home_view(game_state, real_team)
            if flipped:
                team_id = 0

            # AFC: protocol check for match day. Key NAMES only (never values): a different envelope
            # than {"prompt": {"gameState", "teamId", "myPlayers"}} would otherwise play blind silently.
            missing = [k for k in ("gameState", "teamId", "myPlayers") if k not in prompt_data]
            state_ok = isinstance(game_state, dict) and bool(game_state.get("players")) and "ball" in game_state
            if missing or not state_ok:
                gs_keys = sorted(game_state)[:20] if isinstance(game_state, dict) else type(game_state).__name__
                log.warn(f"{PROTOCOL_MARKER}{position_label} missing={missing} payload keys={sorted(payload)} "
                         f"prompt keys={sorted(prompt_data)} gameState keys={gs_keys}")

            # Honor myPlayers from payload if present, otherwise use configured player ID
            my_players = prompt_data.get("myPlayers", [my_player_id])
            effective_pid = my_players[0] if my_players else my_player_id

            def send(cmds) -> str:
                cmds = no_backward_carry(cmds, game_state, team_id, effective_pid)
                cmds = gk_leash(limit_sprint(cmds, game_state, team_id, effective_pid), game_state, team_id, effective_pid)
                return json.dumps(to_server_frame(from_home_view(cmds, flipped, real_team), scale))

            state_summary = summarize_state(
                game_state, team_id, effective_pid, position_label
            )
            log.info(f"{position_label} agent invoked for team {real_team}, controlling player {effective_pid}"
                     + (f" (server frame, scale {scale:.2f})" if scale != 1.0 else "")
                     + (" (home view)" if flipped else ""))

            # AFC: a deterministic rule may answer without the model (validated like a model reply).
            ctx = context_from_state(game_state, team_id, effective_pid)
            forced = rule_fn(game_state, team_id, effective_pid) if rule_fn else None
            if forced:
                checked = validate_reply(forced, ctx)
                if checked.ok:
                    log.info(f"{position_label} answered by rule {rule_fn.__name__}, model not called: "
                             f"{[c.get('commandType') for c in forced]} {json.dumps([c.get('parameters') for c in forced])}")
                    yield send(forced)
                    return
                log.error(f"Rule {rule_fn.__name__} output failed validation ({_issues_text(checked)}), "
                          f"asking the model")

            response = agent(state_summary)
            response_text = str(response)

            def on_recovered(raw: str) -> None:
                # The model wrote Python-flavoured JSON (usually `True`/`False`/`None`).
                # We recovered it rather than dropping the command and falling back —
                # logged so you can see how often your model does this.
                log.warn(f"{position_label} recovered malformed JSON from the model: {raw[:200]}")

            # AFC: validate the raw reply (structure, parameters, ids, pitch bounds, possession)
            # instead of letting parse_commands repair it; an invalid reply is never "fixed".
            verdict = validate_reply(extract_reply(response_text, on_recovered), ctx)

            if verdict.ok:
                commands = list(verdict.commands)
                log.info(f"LLM returned {len(commands)} commands: "
                         f"{[c.get('commandType') for c in commands]}")
                yield send(commands)
            else:
                log.warn(f"LLM reply rejected by validator ({_issues_text(verdict)}), "
                         f"using fallback. Response: {response_text[:200]}")
                commands = fallback_fn(game_state, team_id, effective_pid)
                checked = validate_reply(commands, ctx)  # AFC: the fallback is held to the same contract
                if checked.ok:
                    log.info(f"Fallback returned {len(commands)} commands")
                    yield send(commands)
                else:
                    log.error(f"Fallback output failed validation ({_issues_text(checked)}), "
                              f"using last resort")
                    cmd = dict(last_resort)
                    cmd["teamId"] = team_id
                    yield send([cmd])

        except Exception as e:
            log.error(f"{position_label} agent error: {e}")
            try:
                prompt_data = json.loads(payload.get("prompt", "{}"))
                team_id = prompt_data.get("teamId", 0)
                my_players = prompt_data.get("myPlayers", [my_player_id])
                effective_pid = my_players[0] if my_players else my_player_id
                gs, sc = to_design_frame(prompt_data.get("gameState", {}))  # AFC
                gs, fl = to_home_view(gs, team_id)
                commands = fallback_fn(gs, 0 if fl else team_id, effective_pid)
                yield json.dumps(to_server_frame(from_home_view(commands, fl, team_id), sc))
            except Exception:
                cmd = dict(last_resort)
                cmd["teamId"] = 0  # best guess when payload parsing also failed
                yield json.dumps([cmd])

    return invoke
