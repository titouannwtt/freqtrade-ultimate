"""Une ligne groupée par timeframe pour les bougies périmées, pas une par paire.

Troisième membre de la même famille que `_note_missing_data` et
`IStrategy._note_outdated_pair`. L'étranglement d'origine était HORAIRE PAR
PAIRE, ce qui reste des centaines de lignes quand la whitelist en compte des
centaines : 1103 occurrences relevées dans un échantillon de journaux de flotte.

Le regroupement est clé par TIMEFRAME parce que c'est là qu'est l'information.
« 40 paires en 2h ont plus de 3 bougies de retard » désigne un flux ; une paire
isolée ne dit rien.
"""

import logging
from types import SimpleNamespace

from freqtrade.data.dataprovider import DataProvider


def _dp(monkeypatch, clock):
    dp = DataProvider.__new__(DataProvider)
    dp._DataProvider__stale_pending = {}
    dp._DataProvider__stale_pending_since = 0.0
    dp._DataProvider__stale_last_flush = 0.0
    monkeypatch.setattr("time.monotonic", lambda: clock.t)
    return dp


def _lines(caplog):
    return [r.message for r in caplog.records if "Stale candle data" in r.message]


def test_rien_avant_la_fenetre_de_regroupement(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
    assert _lines(caplog) == []


def test_une_ligne_par_timeframe_jamais_une_par_paire(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(6):
            dp._note_stale_candles(f"P{i}/USDC:USDC", "2h", 3.0 + i)
        dp._note_stale_candles("AAA/USDC:USDC", "15m", 9.9)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("LAST/USDC:USDC", "2h", 99.9)

    lines = _lines(caplog)
    assert len(lines) == 2, "un flux, une ligne"
    assert any("on 2h for 7 pair(s)" in ln for ln in lines)
    assert any("on 15m for 1 pair(s)" in ln for ln in lines)


def test_le_pire_retard_mene_et_sert_de_titre(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(dp.NODATA_MAX_NAMES + 4):
            dp._note_stale_candles(f"P{i:02d}/USDC:USDC", "2h", 2.0 + i)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("WORST/USDC:USDC", "2h", 500.0)

    line = _lines(caplog)[0]
    assert "worst 500.0 candles behind" in line
    assert "WORST/USDC:USDC (500.0)" in line
    # 16 paires + la pire = 17 relevées, 12 affichées, donc 5 repliées.
    assert "and 5 more" in line, "une troncature muette fausserait l'ampleur"
    assert "P00/USDC:USDC" not in line, "ce sont les moins en retard qu'on coupe"


def test_une_paire_garde_son_pire_retard(monkeypatch, caplog):
    """Le pic est le fait ; une lecture plus basse ensuite ne doit pas l'effacer."""
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 42.0)
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 2.5)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
    assert "(42.0)" in _lines(caplog)[0]


def test_etrangle_mais_jamais_reduit_au_silence(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
        clock.t += 120
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
        assert len(_lines(caplog)) == 1

        clock.t += dp.NODATA_REPORT_INTERVAL_S
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
    assert len(_lines(caplog)) == 2


def test_le_lot_se_vide_apres_parution(monkeypatch, caplog):
    clock = SimpleNamespace(t=1000.0)
    dp = _dp(monkeypatch, clock)
    with caplog.at_level(logging.WARNING):
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
        clock.t += dp.NODATA_BATCH_WINDOW_S + 1
        dp._note_stale_candles("BTC/USDC:USDC", "15m", 3.0)
    assert dp._DataProvider__stale_pending == {}
