# Canonical opening-event schema

Every league adapter must emit:

```text
game_id, date, home_team, away_team
opening_control_event
  winning_team_id
  possession_player_id (nullable)
  raw_text
first_attempt
  team_id, athlete_id, clock, made, points_attempted, raw_text
first_field_goal
  team_id, athlete_id, clock, assisted_by_id, raw_text
first_points
  team_id, athlete_id, clock, score_value, raw_text
first_attempt_by_team[team_id]
first_field_goal_by_team[team_id]
opening_plays[]
```

Preserve raw event text for reconciliation. Never infer a player name solely from prose when stable participant IDs exist.

WNBA mapping:

- opening control: jump-ball play and gaining-possession participant
- attempt: `shootingPlay=true`
- made field goal: `shootingPlay=true`, `scoringPlay=true`, `scoreValue>=2`
- first points: first `scoringPlay=true`, `scoreValue>0`
