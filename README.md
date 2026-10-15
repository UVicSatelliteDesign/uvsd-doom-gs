# uvsd-doom-gs
Ground station software for playing and displaying DOOM on a balloon

## Development
[![CI](https://github.com/UVicSatelliteDesign/uvsd-doom-gs/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/UVicSatelliteDesign/uvsd-doom-gs/actions/workflows/ci.yml)

To run the app, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run
```bash
uv run ground_station/app.py
```

To test the code, run:

```bash
uv run ruff check .     # Lint (add --fix to apply safe fixes)
uv run mypy             # Type check
uv run pytest           # Checks tests in tests/
```
