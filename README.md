# uvsd-doom-gs
Ground station software for playing and displaying DOOM on a balloon

## Development
[![CI](https://github.com/UVicSatelliteDesign/uvsd-doom-gs/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/UVicSatelliteDesign/uvsd-doom-gs/actions/workflows/ci.yml)

Requires [uv](https://docs.astral.sh/uv/). CI runs the same two commands on every push and PR to `main`:

```bash
uv run ruff check .     # lint (add --fix to apply safe fixes)
uv run mypy             # type check (config in pyproject.toml)
uv run pytest           # tests in tests/, no display needed
```
