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

NBA mapping:

- Identical to the WNBA mapping above — the NBA rides the same ESPN
  basketball play-by-play feed, so the adapter is the league config in
  `lib/leagues.ts` (endpoint, team set, display abbreviations, headshot
  base, season window) rather than a second parser.
- Validated by `tests/nba-model.test.ts` against a known-game fixture
  covering the jump ball, the first attempt, the miss branch, a free throw
  that scores the first POINTS without being the first FIELD GOAL, and each
  team's own first basket.
- Season window crosses New Year: `defaultSeasonStart` resolves a January
  slate back to the previous October.
