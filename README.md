# the-board-system

## MLB research / parlay layer

The MLB export now includes a `research_board` section inside `mlb.json`.

It is designed to sit on top of the core model, not replace it:

- `home_run` with `hr_of_day` and 2 / 3 / 4 / 6 leg parlays
- separate `hits`, `total_bases`, and `strikeouts` boards
- optional outside-research overlay via `backend/data_raw/mlb_research_notes.json`

Use `backend/data_raw/mlb_research_notes.example.json` as the template for any manual source notes
such as X, TeamRankings, beat writers, lineup notes, or weather notes.
