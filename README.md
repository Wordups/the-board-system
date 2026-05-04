# the-board-system

## MLB research / parlay layer

The MLB export now includes a `research_board` section inside `mlb.json`.

It is designed to sit on top of the core model, not replace it:

- `home_run` with `hr_of_day` and 2 / 3 / 4 / 6 leg parlays
- separate `hits`, `total_bases`, and `strikeouts` boards
- optional outside-research overlay via `backend/data_raw/mlb_research_notes.json`

Use `backend/data_raw/mlb_research_notes.example.json` as the template for any manual source notes
such as X, TeamRankings, beat writers, lineup notes, or weather notes.

## NBA research / stack layer

The NBA export now also includes a `research_board` section inside `nba.json`.

It is organized for:

- `top_strip` for best stacked plays across the top
- `safe_plays` for higher-floor combinations
- `long_shots` for higher-variance money plays
- sectioned boards for `PTS`, `AST`, `REB`, and `3PM`

Use `backend/data_raw/nba_research_notes.example.json` as the template for any outside notes
such as minutes caps, rotation changes, matchup notes, or manual long-shot tags.
