# Contributing to mcp-approval-proxy

Thanks for your interest in contributing!

## Setup

1. Clone the repository
2. Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Testing

Run the test suite:

```bash
uv run pytest
```

Run tests for a specific module:

```bash
uv run pytest tests/test_engines.py -v
```

## Linting and Formatting

Check code quality:

```bash
uv run ruff check .
```

Format code:

```bash
uv run ruff format .
```

## Submitting Changes

1. Create a feature branch from `main`
2. Make your changes
3. Run tests and linting to ensure they pass
4. Commit with clear, descriptive messages
5. Push to your fork and open a pull request

## Guidelines

- Keep commits focused and atomic
- Add tests for new functionality
- Update documentation as needed
- Follow the existing code style (enforced by ruff)
- All tests must pass before merging

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for code quality. Configuration is in `pyproject.toml`.

### Key rules:
- Line length: 100 characters
- Python 3.11+
- Type hints encouraged but not required
- Prefer clarity over cleverness
