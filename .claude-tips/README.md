# .claude-tips/ — Routing table

Ce répertoire contient les règles de trading algorithmique distillées dans `tips.txt` (199 tips, sources : Carver, Clenow, Chan, Lopez de Prado, communauté Freqtrade France), organisées par catégorie pour consultation rapide.

## Comment utiliser (pour Claude)

1. Identifie le type de demande dans la table ci-dessous
2. Lis les fichiers correspondants AVANT de répondre
3. Règles strictes (🚫) : ne jamais enfreindre sans avertissement explicite
4. Bonnes pratiques (✅) : suivre par défaut, justifier toute exception
5. Conseils avancés (💡) : évaluer si le contexte s'applique

## Table de correspondance

| Demande utilisateur | Fichiers à consulter |
|---|---|
| Analyse/review une stratégie | `strategy_evaluation.md`, `risk_management.md`, `strategy_bugs_reference.md` + fichier type (mean_reversion / trend_following / machine_learning) |
| Crée/code une stratégie | `strategy_development.md`, `data_quality.md`, `risk_management.md` + fichier type |
| Lance/analyse un backtest | `backtesting.md`, `data_quality.md`, `strategy_evaluation.md` |
| Lance un hyperopt / loss function | `hyperopt.md`, `backtesting.md` |
| Diagnostic bots en live | `live_trading.md`, `risk_management.md`, `strategy_evaluation.md` |
| Optimise une stratégie | `hyperopt.md`, `strategy_development.md`, `psychology.md` |
| Config (throttle, pricing, pairlist, timeout) | `live_trading.md` |
| Sizing / DCA / leverage | `risk_management.md`, `live_trading.md`, fichier type |
| Filtre / pairlist / régime de marché | `market_analysis.md`, `live_trading.md` |
| Multi-bot / portfolio / corrélation | `portfolio.md`, `risk_management.md` |
| Discussion ML / FreqAI | `machine_learning.md`, `data_quality.md` |
| Question conceptuelle | `psychology.md` + fichier thématique |

## Contexte par défaut

DCA mean-reversion (long oversold + short overbought) sur Hyperliquid USDC perps en 15m. En cas de doute : `mean_reversion.md` > `trend_following.md`.

## Source de vérité

`tips.txt` à la racine du repo. Les fichiers `.claude-tips/` sont des index actionnables. En cas de divergence, `tips.txt` prime.
