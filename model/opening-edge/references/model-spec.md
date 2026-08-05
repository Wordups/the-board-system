# Model specification

## Candidate score

| Component | Weight | Required evidence |
|---|---:|---|
| Player whole-game first-basket share | 30% | first field goals / team games |
| Team first-basket rate | 20% | team scores first / team games, with home/away when available |
| First-shot involvement | 15% | player's team-first attempts / team games plus conversion |
| Opening-possession matchup | 15% | team tip rate and opponent tip resistance |
| Role and availability | 10% | confirmed starter, health, minutes, trade/return context |
| Head-to-head opening sequences | 5% | prior matchup sequences; supporting evidence only |
| Market value | 5% | current odds and implied probability |

Use neutral `0.5` only for a missing component and label it unverified. Do not present the score as win probability.

## Odds

For positive American odds `+A`:

- implied probability = `100 / (A + 100)`
- decimal odds = `1 + A / 100`
- independent cross-game parlay decimal = product of leg decimals
- return = stake × decimal odds

Keep sportsbook boosts separate from mathematical price.

## Minimum audit

- Team games equal schedule sample after exhibitions are excluded.
- Player whole-game first baskets sum to each team's team-first total.
- Exactly one whole-game first field goal exists per completed game.
- A player cannot have more made first attempts than first attempts.
- Market definition is explicit.
- Current starters, injuries and prices have timestamps.
