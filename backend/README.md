# The Board System Backend

This backend owns data collection, normalization, scoring, board assembly, schema validation, JSON export, and Git publishing.

Current status:

- MLB pipeline implemented end to end
- Other sports scaffolded as placeholders

Run the MLB pipeline:

```powershell
python backend/scripts/run_mlb.py
```

Run tests:

```powershell
python -m pytest backend/tests
```
