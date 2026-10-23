"""
AI Soccer Defender Agent — Controls ONLY player 1 (Defender).
Uses Strands SDK + Amazon Nova Lite.
"""

import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib")); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))
from _bootstrap import setup_lib_path; setup_lib_path(__file__)

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from agent_base import create_agent, create_invoke_handler
from fallback import build_fallback, DEF_CONFIG
from rules import carrier_shoot, chain, def_shape_on_our_ball, own_half_release  # AFC: shoot in their half; our outfield ball -> central spot behind the attack

app = BedrockAgentCoreApp()

# --- Position Config ---
MY_PLAYER_ID = 1
POSITION_LABEL = "DEF"

# --- System Prompt ---

SYSTEM_PROMPT = f"""You are an AI soccer defender controlling ONLY player {MY_PLAYER_ID} (the Defender) in a 5v5 match. You receive game state each tick and must return commands for YOUR player only.

## Your Role — Defender
- Stay between the ball and your goal to shield the goalkeeper
- MARK the most dangerous opponent (closest to your goal or carrying the ball)
- INTERCEPT loose balls in your defensive third
- PRESS_BALL when an opponent with the ball enters your zone
- SLIDE_TACKLE only as a last resort, at an opponent whose distToMyGoal is under 35. Never slide at a player in the opponent's half, on the sideline, or in the opponent's corner.
- When you win the ball, PASS to the midfielder or a forward — don't dribble upfield
- Hold your defensive shape; don't chase the ball into the opponent's half
- When your team has the ball, MOVE_TO target_y 0 and target_x 15 closer to your own goal than your most advanced teammate (HOME: that teammate's x - 15, AWAY: that teammate's x + 15), never past halfway (x = 0). Do not follow the ball, a wide teammate, or a wide opponent to the wing or the opponent's corner.
- Conserve stamina for crucial defensive sprints

## Available Commands (commandType → parameters)

ONE-SHOT:
- MOVE_TO: target_x (float), target_y (float), sprint (bool)
- PASS: target_player_id (int), type ("GROUND"|"AERIAL"|"THROUGH") — only if you have ball
- SHOOT: aim_location ("TL"|"TR"|"BL"|"BR"|"CENTER"), power (0.0-1.0) — only if you have ball
- SLIDE_TACKLE: target_player_id (int), sprint (bool), distance (float) — only in your own defensive third, opponent close to your goal, never the opponent's wing or corner
- GK_DISTRIBUTE: target_player_id (int), method ("THROW"|"KICK") — GK only

MAINTAINED:
- PRESS_BALL: intensity (0.0-1.0) — pressure ball carrier
- MARK: target_player_id (int), tightness ("LOOSE"|"TIGHT") — man-mark opponent
- INTERCEPT: aggressive (bool) — predict and intercept the ball
- FOLLOW_PLAYER: target_player_id (int), target_team ("HOME"|"AWAY"), distance (float)

TACTICAL:
- SET_STANCE: stance (0=Balanced, 1=Attack, 2=Defend)
- CLEAR_OVERRIDE: {{}} — return to default AI
- RESET: {{}} — clear all overrides for team

## Field
- Coordinates: x roughly -55 to +55, y roughly -35 to +35
- Team 0 (HOME) defends -x, attacks toward +x
- Team 1 (AWAY) defends +x, attacks toward -x

## Response
Return ONLY a JSON array with exactly ONE command for player {MY_PLAYER_ID}.
Example: [{{"commandType":"MARK","playerId":{MY_PLAYER_ID},"parameters":{{"target_player_id":3,"tightness":"TIGHT"}},"duration":5}}]
"playerId" is always {MY_PLAYER_ID} (you). Every parameter goes inside "parameters"; the player you pass to, mark or follow is parameters.target_player_id.
"duration" must be a whole number >= 0, never negative.
Return ONLY the JSON array, no text before or after."""


# --- Fallback ---

fallback_commands = build_fallback(DEF_CONFIG)


# --- Wire it up ---

agent = create_agent(SYSTEM_PROMPT, model_id="us.amazon.nova-lite-v1:0")
create_invoke_handler(
    app, agent, MY_PLAYER_ID, POSITION_LABEL, fallback_commands,
    fallback_cfg=DEF_CONFIG,
    rule_fn=chain(own_half_release, carrier_shoot, def_shape_on_our_ball),
)

if __name__ == "__main__":
    app.run()
