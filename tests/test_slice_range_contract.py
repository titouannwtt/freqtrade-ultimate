"""`slice_range` rend un ndarray, et la trame reste du JSON inchangé.

Ce contrat existe pour une raison de performance mesurée, pas par goût : une fois
`orjson` en place, le `.tolist()` qui terminait `slice_range` est devenu le
premier consommateur de CPU du daemon (64 % d'un profil de 2034 échantillons,
contre 18,5 % pour tout le sérialiseur). Convertir 5000x6 float64 en 30 000
flottants Python boîtés, uniquement pour que le sérialiseur les reparcoure
aussitôt, est du gaspillage pur.

Le piège que ces tests gardent : un ndarray n'a pas de valeur de vérité. Un
`if not rows` réintroduit ailleurs lèverait `ValueError` dès qu'il y a plus d'un
élément, et seulement en production, sur une série non vide.
"""

import json

import numpy as np

from freqtrade.ohlcv_cache import protocol
from freqtrade.ohlcv_cache.store import CandleSeries


def _series(rows):
    s = CandleSeries.__new__(CandleSeries)
    s.candles = np.array(rows, dtype="float64")
    return s


BLOCK = [
    [1000.0, 1, 2, 3, 4, 5],
    [2000.0, 6, 7, 8, 9, 10],
    [3000.0, 11, 12, 13, 14, 15],
]


def test_slice_range_rend_un_ndarray_pas_une_liste():
    rows = _series(BLOCK).slice_range(0, 9999)
    assert isinstance(rows, np.ndarray), "le .tolist() ne doit pas revenir"


def test_la_trame_json_est_identique_au_contrat_client():
    """Les bots ne changent pas : ils doivent relire exactement les mêmes octets."""
    rows = _series(BLOCK).slice_range(1500, 3500)
    back = json.loads(protocol.dumps({"ok": True, "data": rows}).decode())
    assert back["data"] == [
        [2000.0, 6.0, 7.0, 8.0, 9.0, 10.0],
        [3000.0, 11.0, 12.0, 13.0, 14.0, 15.0],
    ]


def test_une_plage_vide_se_serialise_en_liste_vide():
    rows = _series(BLOCK).slice_range(50_000, 60_000)
    assert rows.size == 0
    assert json.loads(protocol.dumps({"data": rows}).decode())["data"] == []


def test_une_serie_vide_ne_leve_pas():
    s = CandleSeries.__new__(CandleSeries)
    s.candles = np.empty((0, 6))
    rows = s.slice_range(0, 1)
    assert rows.size == 0
    assert json.loads(protocol.dumps({"data": rows}).decode())["data"] == []


def test_la_vacuite_se_teste_par_size_jamais_par_verite():
    """Le piège, rendu explicite : `not rows` lève sur plus d'un élément."""
    rows = _series(BLOCK).slice_range(0, 9999)
    assert rows.size > 0
    try:
        bool(rows)
    except ValueError:
        return
    raise AssertionError("un ndarray multi-éléments doit refuser bool() : garde toujours .size")


def test_le_daemon_teste_bien_par_size():
    """Verrou textuel : le site d'appel ne doit pas retomber sur `not data_rows`."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "freqtrade" / "ohlcv_cache" / "daemon.py"
    body = src.read_text(encoding="utf-8")
    assert "data_rows.size == 0" in body
    assert "if not data_rows and errors" not in body, "l'ancienne garde lèverait en production"
