# apebot

Autonomous paper-then-live trader for tokenized-stock premiums on Robinhood Chain.

- Start with `docs/HANDOFF.md`: facts with confidence levels, strategy spec, assumptions to verify,
  open questions. `README.md` is the operator guide.
- Dev: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`,
  then `pytest` and `ruff check src tests`. `apebot simulate --days 7` is an offline smoke test.
- The code has never run against the real chain; contract addresses in `config.example.yaml` are
  blank on purpose. `apebot check` validates them.
- Keep `strategy.py` pure (no I/O) and keep the paper and live paths identical except for the executor.
