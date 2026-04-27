# Market Analysis — Tips et garde-fous

## Règles strictes (ne jamais enfreindre)

- 🚫 **Phases de marché (S1-S4 de Beetcoin) pour sizing et risk management UNIQUEMENT, JAMAIS pour les conditions d'entrée/sortie** (overfitting garanti). (tips.txt #30, Beetcoin)
- 🚫 **Ne jamais utiliser un filtre de régime binaire (risk on / risk off)**. Scale-in/scale-out progressif. Switch binaire "vend tout / rachète tout" = faux signaux coûteux + re-entries tardives. Quand tout semble catastrophique, commencer à scale in légèrement (contrarien à l'extrême). (tips.txt #89, Clenow)

## Bonnes pratiques (toujours suivre sauf justification explicite)

- ✅ **En période de congestion / faible volatilité, seules les petites TF performent**. Mais la vol peut revenir brutalement et effacer des mois de gains en quelques heures. Ne jamais mettre tout son capital sur une stratégie 5m "parce qu'elle performe depuis 2 semaines". (tips.txt #31, Beetcoin)
- ✅ **Stage Analysis (S1-S4) comme framework de classification des phases**. S1 = accumulation, S2 = markup, S3 = distribution, S4 = markdown. En S2 long performe, en S4 short performe. En S1/S3 réduire l'exposition. (tips.txt #43, Beetcoin)
- ✅ **Crypto = pure psychologie**. "Fundamental value is zero. So it's all a matter of psychology." C'est précisément pour ça que c'est tradeable: les trends sont amplifiées par le herding behavior. Trader comme instrument de pure psychologie/momentum, jamais comme investissement fondamental. (tips.txt #91, Clenow)
- ✅ **Filtre min-momentum plutôt que filtre régime binaire**. Appliquer un seuil de momentum minimum, et si peu de pairs passent, basculer partiellement en cash. Portfolio peut être 67% stocks / 33% bonds selon signaux qualifiants. (tips.txt #126, Clenow)
- ✅ **Les régime shifts macro ne sont PAS arbitrageables**. "Interest rate is something that will affect trading but trading doesn't affect it." ML peut apprendre à prédire les régimes macro sans que sa propre réussite détruise le signal — contrairement aux signaux de prix. Ajouter taux/inflation/credit comme features externes ne se "brûle" pas avec l'adoption. (tips.txt #149, Chan)
- ✅ **Bull market = contreparties faibles, bear market = contreparties pro**. En bull tu gagnes facilement contre newbies. En bear tu te bats contre pros, edge dilué. Attendre peu en bull, ÊTRE BEAUCOUP PLUS SÉLECTIF en bear. (tips.txt #184, SalsaTekila)

## Conseils avancés (à appliquer selon le contexte)

- 💡 **Analyse intermarché DXY / ALT/BTC / TOTAL3/BTC** — Applicable quand: tu veux des filtres macro. DXY bearish = bullish pour risk assets. ALT/BTC = thermomètre du risk appetite crypto. TOTAL3/BTC qui ne break pas = pas de vrai altseason. Modulation d'exposition, pas entry signals. (tips.txt #45, Beetcoin)
- 💡 **Bars > candles pour l'analyse technique** — Applicable quand: analyse manuelle des graphiques. La couleur d'une bougie dépend d'open vs close, n'apporte aucune info. Les bars (OHLC sans couleur) forcent à lire la structure réelle. (tips.txt #46, Beetcoin)
- 💡 **RSI 30 périodes (pas 14)** — Applicable quand: TF très hautes 3d/1w où les indicateurs classiques ont encore un edge (niveau de Beetcoin). Sur TF basses 15m-4h, cet ajustement n'apporte rien — préférer indicateurs custom (cf. #42). (tips.txt #47, Beetcoin)
- 💡 **VIX comme signal contrarian** — Applicable quand: stratégie sur actions US. Acheter quand VIX 5%+ au-dessus MA(10), vendre quand 5%+ en dessous. En crypto, DVOL Deribit ou implied vol BTC peut jouer un rôle similaire mais données moins propres. (tips.txt #63, Connors)
- 💡 **Structural breaks offrent les meilleurs ratios risk/reward** — Applicable quand: tu peux détecter les ruptures (tests CUSUM). Exploiter avant que le marché ne s'ajuste. (tips.txt #83, Lopez de Prado)
- 💡 **ETF spot Bitcoin/ETH ont changé la microstructure post-2024** — Applicable quand: stratégie sur BTC/ETH spot (Coinbase, Binance spot). Gros blocs institutionnels écrasent setups scalping classiques. Sur Hyperliquid perps, impact moins direct. Recalibrer sur données post-Jan 2024. (tips.txt #186, SalsaTekila)
- 💡 **Credit spreads investment-grade > 400bps = canari risk-off global** — Applicable quand: tu veux un filtre macro externe non-arbitrageable. Si > 400bps, réduire exposition ou basculer conservateur. Si < 200bps, environnement risk-on. (tips.txt #199, MacroAlf)
