# NFL Parlay Generation Pipeline

## Overview

The Sports Engine generates real NFL same-game parlays from live player props data. The system is designed to:

1. **Fetch real player props** from FanDuel (or other sources)
2. **Identify star players** per game (QB, RB1, RB2, WR1, TE)
3. **Generate three ticket types** per game:
   - **Grind**: 4 pure volume legs (PassYds, RecYds, RushYds, REC) - high confidence, low risk
   - **Moonshot**: 14 total legs (3 star player TDs + 11 volume legs) - lottery odds
   - **TD Parlay**: 3 star player any-time TDs - dedicated TD-only ticket

## Architecture

### Data Pipeline

```
FanDuel Props (Live)
    ↓
FanDuelScraper.fetch_game_props()
    ↓
[Player Props Data]
    ↓
build_same_game_parlays_for_game()
    ↓
[Grind + Moonshot + TD Parlay Tickets]
    ↓
frontend/sports-engine.html (Display)
```

### Files

- **`backend/app/data/fanduel_scraper.py`**
  - Fetches live props from FanDuel sportsbook
  - Normalizes markets (PassYds, RecYds, RushYds, REC, TD)
  - Handles JS rendering and network errors

- **`backend/app/sim/nfl_parlays.py`**
  - Core parlay generation engine
  - Star player identification (top performers by position)
  - Grind/Moonshot/TD logic
  - Anti-correlation gates (prevents same-team RB+QB pairs)

- **`backend/app/builders/nfl_parlays_builder.py`**
  - `build_nfl_parlays_board_from_fanduel()` - FanDuel integration
  - `build_nfl_parlays_board()` - ESPN data fallback
  - Orchestrates prop fetching → parlay generation → output

- **`backend/scripts/generate_parlays_from_fanduel.py`**
  - CLI: Generate parlays from live FanDuel props
  - Usage: `python generate_parlays_from_fanduel.py "CAR@ATL" "PHI@TEN"`

- **`backend/scripts/generate_parlays_from_props_data.py`**
  - CLI: Generate parlays from custom props JSON
  - Usage: `python generate_parlays_from_props_data.py --data props.json`

- **`frontend/sports-engine.html`**
  - Display page for Sports Engine tab
  - Shows all three ticket types with expandable legs
  - Game-script indicators (shootout/blowout/normal)
  - Confidence scores on each leg

## Usage Examples

### 1. Generate from FanDuel (Live)

```bash
cd backend
python scripts/generate_parlays_from_fanduel.py "CAR@ATL" "PHI@TEN" "MIA@SF" "IND@KC" \
  --output ../frontend/data/nfl_parlays.json
```

This will:
- Scrape live props from FanDuel for each game
- Run parlay generator on real player data
- Output with actual player names and FanDuel odds
- Save to `frontend/data/nfl_parlays.json`

### 2. Generate from Props Data

Create `props.json`:
```json
{
  "CAR_ATL": {
    "matchup": "CAR @ ATL",
    "time": "1:00 PM EDT",
    "candidates": [
      {
        "player_name": "Bryce Young",
        "team": "CAR",
        "position": "QB",
        "market": "PassYds",
        "line": 250,
        "odds": 1.5,
        "confidence": 90,
        "score": 85
      },
      ...
    ]
  }
}
```

Then run:
```bash
cd backend
python scripts/generate_parlays_from_props_data.py --data props.json
```

### 3. Integrate into The Board

The parlay data is automatically served via the Sports Engine tab:
- URL: `http://localhost:8000/frontend/sports-engine.html`
- Data source: `frontend/data/nfl_parlays.json`
- Display: Grind tickets | Moonshot tickets (expandable) | Star Player TD Parlays

## Output Format

Each game ticket contains:

```json
{
  "game_id": "CAR_ATL",
  "matchup": "CAR @ ATL",
  "time": "1:00 PM EDT",
  "game_script": "normal",
  "grind": {
    "legs": [4 volume legs],
    "odds": 650,
    "stake": 100,
    "implied_win": 750
  },
  "moonshot": {
    "legs": [14 legs: 3 star TDs + 11 volume],
    "odds": 2150,
    "stake": 25,
    "implied_win": 562
  },
  "td_parlay": {
    "legs": [3 star player TDs],
    "odds": 3000,
    "stake": 50,
    "implied_win": 1550
  }
}
```

## Leg Structure

### Grind Legs (4 total)
- Pure volume only: PassYds, RecYds, RushYds, REC
- No any-time TDs
- High confidence (85%+)
- Odds: +650 to +700 (7-8x return)

### Moonshot Legs (14 total)
- **3 Star Player Legs**: Any-time TDs for QB, RB1, WR1
- **11 Volume Legs**: Mix of PassYds, RushYds, RecYds, REC across both teams
- Cross-team stacking (Dolphins QB passes + 49ers RB rushes)
- Odds: +2150 to +2500 (22-26x return)

### TD Parlay Legs (3 total)
- Any-time touchdowns ONLY
- Star players: QB, RB1, WR1 (highest confidence per position)
- Odds: +3000 to +3400 (31-35x return)

## Game Script Logic

- **Shootout** (total ≥ 50): Volume thresholds relaxed, more passing yards
- **Blowout** (spread ≥ 7): RB focus, heavy rushing legs
- **Normal**: Standard thresholds

## Star Player Identification

Identifies top performer per position per team:
- **QB**: Highest passing yards/TD score
- **RB1**: Highest rushing yards/receptions score
- **RB2**: Second-best RB (if available)
- **WR1**: Highest receiving yards/TD score
- **TE**: Highest TE receiving score

These are the ONLY players eligible for TD parlay legs.

## Anti-Correlation Gates

Prevents invalid pairings:
- ❌ Same-team RB + QB in moonshot (run script kills passing)
- ✅ Cross-team pairings (CAR QB + ATL RB = positive correlation)
- ✅ Volume-only combos (no TD scoring conflicts)

## Network Constraints

The FanDuel scraper gracefully handles network limitations:
- Primary: Playwright-based JS rendering for live props
- Fallback: Returns empty candidates if network unavailable
- Alternative: Use `generate_parlays_from_props_data.py` with manual data

## Future Enhancements

- [ ] DraftKings props scraping (alternative source)
- [ ] Historical parlay performance tracking
- [ ] User-customizable confidence thresholds
- [ ] Live betting integration (place parlays directly)
- [ ] Injury status filtering
- [ ] Weather/wind adjustments
