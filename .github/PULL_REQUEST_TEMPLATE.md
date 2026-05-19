<!-- Thanks for contributing to Freqtrade Ultimate. Please fill in the relevant sections below. -->

## Summary

<!-- What does this PR change? One sentence. -->

## Type

- [ ] Bug fix (fork-specific code)
- [ ] New feature (infrastructure / hyperopt / Hyperliquid / observability)
- [ ] New showcase strategy in `user_data/strategies/`
- [ ] Documentation improvement
- [ ] Refactor (no behavior change)

## Linked issue

<!-- Closes #XYZ. If no issue exists for non-trivial changes, please open one first. -->

## Testing

<!-- How did you verify this works? `pytest` runs, manual repro, live bot, etc. -->

## Checklist

- [ ] Code lints clean (`ruff check freqtrade/` and `ruff format --check freqtrade/`)
- [ ] Relevant tests added / updated
- [ ] `docs/FEATURES.md` updated if behavior changed
- [ ] Commit message follows convention (`feat:`, `fix:`, `docs:`, etc., no AI co-author trailers)

## Strategy contributions only

If this PR adds a strategy in `user_data/strategies/`, confirm:
- [ ] `<strategy>.py` (code), `<strategy>_readme.md` (philosophy + config), `<strategy>_analysis.md` (backtest + walk-forward + PBO), `<strategy>.json` (params) all present
- [ ] Walk-forward analysis ran with `freqtrade walk-forward` in CPCV mode
- [ ] PBO score reported and below 0.5
- [ ] Honest drawdown profile included (no cherry-picking)
