# Mean Reversion (incl. DCA) — Tips et garde-fous

> Couvre les stratégies DCA mean-reversion (long oversold + short overbought) sur perps ou spot. Lire ce fichier avant toute discussion sur des stratégies mean-reversion.

## Règles strictes (ne jamais enfreindre)

- 🚫 **Le stop-loss destructif détruit le mean-reversion**. Couper une position en perte = retirer précisément le mécanisme qui devait ramener le prix. En crypto, un SL ~30% (touché 1-3% du temps) protège des catastrophes sans détruire l'edge. Le SL doit être une assurance rare, pas un outil d'optimisation. Couper "rapidement" reste possible via exits TEMPORELS (max hold) ou INDICATEURS (retournement signal), PAS via SL fixe serré. (tips.txt #40, communauté)
- 🚫 **Les stop-losses réduisent la performance des stratégies court-terme mean-reversion**. Testé sur centaines de milliers de transactions: ajouter un SL endommage systématiquement la perf. Si SL nécessaire, baser sur volatilité (2x ATR) jamais en pourcentage fixe. (tips.txt #62, Connors)
- 🚫 **Sortir sur close au-dessus de la MA(5) ou sur signal RSI — pas sur TP fixe**. Les meilleurs exits pour mean-rev sont dynamiques. Un TP fixe ne s'adapte pas au régime de marché. (tips.txt #66, Connors)
- 🚫 **Pas de SL pour le mean-reversion, sauf s'il n'est jamais censé être déclenché (catastrophe)**. Les jours de forte vol intraday sont ceux où le mean-rev gagne le plus — et aussi ceux où le SL se déclenche. Le SL coupe précisément les meilleurs trades. Préférer exit temporel (max hold) ou sur indicateur (RSI qui remonte au-dessus de X). (tips.txt #67, Chan)
- 🚫 **"Losers average losers" SAUF si c'est PLANIFIÉ**. Moyenner à la baisse sans plan = destruction. Moyenner avec DCA pré-calibré (niveaux + tailles fixes) = stratégie. La différence est la pré-définition des safety orders, pas l'acte lui-même. Justifie directement le DCA algorithmique avec `safety_order_volume_scale` fixe. (tips.txt #196, Beetcoin)

## Bonnes pratiques (toujours suivre sauf justification explicite)

- ✅ **ROCP (Rate of Change Percentage) = indicateur le plus polyvalent pour le mean-reversion**. Mesure le retracement en % depuis un point de référence. Utilisable comme entrée (retracement > X% = oversold), sortie (rebond > Y% depuis creux), filtre tendance. Un seul indicateur, trois usages. (tips.txt #41, communauté)
- ✅ **Short-term mean-reversion offre plus d'opportunités consistantes que le trend-following pour les petits comptes TF courts**. SR plus élevé, plus accessible. Trend-following nécessite beaucoup de diversification (= capital). Justifie le focus sur DCA oversold/short en timeframes courts (5m-15m). (tips.txt #70, Chan)

## Conseils avancés (à appliquer selon le contexte)

- 💡 **RSI(2) = oscillateur le plus proche du "saint graal" mean-rev sur actions daily** — Applicable quand: actions US daily. Achat RSI(2) < 10, vente RSI(2) > 90. Combiné avec MA(200) comme filtre tendance = 75% winrate sur décennies. Transferabilité crypto: principe (RSI court-période + filtre tendance long-période) tient, mais seuils et périodes à recalibrer sur 15m crypto. (tips.txt #61, Connors)

## Spécifique au DCA (sous-catégorie)

- 🚫 **DCA = "losers average losers" planifié** — niveaux de safety orders et `safety_order_volume_scale` doivent être fixés AVANT l'entrée, jamais ajustés dynamiquement quand le trade va mal. (tips.txt #196)
- 🚫 **Min 4 safety orders effectifs par trade** sinon DCA = all-in déguisé. (tips.txt #110, Carver)
- 🚫 **`max_open_trades` change tout pour DCA**: passer de MOT 5 à 3 double quasiment l'allocation par trade. Toujours fixer délibérément. Voir `live_trading.md` § "Capital & sizing" pour la formule complète et l'effet de compounding. (tips.txt #24)
- 🚫 **Toujours `--timeframe-detail 1m` pour BT/hyperopt DCA** sinon phantom trades. Voir `hyperopt.md` pour le détail. (tips.txt #20)
