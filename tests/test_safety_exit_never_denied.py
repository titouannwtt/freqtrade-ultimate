"""Aucune stratégie ne doit pouvoir refuser une sortie de SÉCURITÉ.

`confirm_trade_exit` est un veto : renvoyer `False` annule la sortie. C'est
légitime pour une sortie DOUCE (signal, ROI) que la stratégie veut retarder. Ça
ne l'est jamais pour un stoploss, un time-stop, une sortie forcée ou une
fermeture externe : une position sous l'eau devient alors impossible à couper,
et seule une liquidation la ferme.

Ce n'est pas théorique. Le 2026-09-02, `coppock_keltner_short_v2` détenait deux
shorts ouverts depuis trois semaines. Son journal montrait la boucle :

    Exit for CFX/USDC:USDC detected. Reason: roi
    User denied exit for CFX/USDC:USDC.

Le bot voyait sa sortie et se l'interdisait, indéfiniment. `retire_bot.py` a
refusé de l'arrêter (à raison : couper un bot qui détient encore des positions
sur un portefeuille netté fabrique des orphelins), et il a fallu fermer les deux
positions on-chain en `reduceOnly`.

Le générateur de stratégies porte la garde depuis le 2026-08-21, mais les
stratégies écrites AVANT ne l'ont pas. Ce test est le filet qui empêche une
telle stratégie de repartir en production sans être vue.
"""

import ast
import re
from pathlib import Path

import pytest


STRATEGY_DIR = Path(__file__).resolve().parents[1] / "user_data" / "strategies"

# Les motifs de sortie qui ne se refusent jamais. Alignés sur le générateur
# (`user_data/strategies_generator/codegen/writer.py`) : les deux listes doivent
# rester identiques, sinon une stratégie générée passerait ce test tout en étant
# vulnérable.
SAFETY_KEYS = ("stop", "timestop", "liquidation", "force", "emergency", "external")


def _deployed_strategy_files():
    """Les stratégies réellement DÉPLOYÉES, c'est-à-dire citées par une config de bot.

    ⚠️ Le périmètre est délibérément restreint. Balayer tout `user_data/strategies`
    fait remonter des centaines de stratégies générées AVANT que le générateur ne
    porte sa garde (mesuré : 332 sur 714). Un test rouge en permanence ne protège
    rien, on cesse de le lire. Le contrat utile est : « aucune stratégie DÉPLOYÉE
    ne peut refuser une sortie de sécurité ». Les nouvelles sont couvertes par
    `test_le_generateur_porte_la_meme_garde`, donc sûres par construction.
    """
    import json

    configs = STRATEGY_DIR.parents[1] / "live_configs"
    if not configs.is_dir():  # pragma: no cover - installation sans flotte
        return
    voulues = set()
    for cfg in sorted(configs.glob("*.json")):
        if cfg.name.startswith("_"):
            continue
        try:
            voulues.add(json.loads(cfg.read_text(encoding="utf-8")).get("strategy"))
        except Exception:  # noqa: S112 - une config illisible ne doit pas casser l'audit
            continue
    voulues.discard(None)
    for p in sorted(STRATEGY_DIR.rglob("*.py")):
        s = str(p)
        if "/archives/" in s or "__pycache__" in s or "backup" in s:
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: S112 - un fichier illisible est ignore, pas fatal
            continue
        if any(f"class {nom}" in src for nom in voulues):
            yield p


def _confirm_trade_exit_body(src: str) -> str | None:
    """Le corps de `confirm_trade_exit`, ou None si la stratégie n'en a pas."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "confirm_trade_exit":
            return ast.get_source_segment(src, node) or ""
    return None


def _can_deny(body: str) -> bool:
    """Vrai si la fonction peut renvoyer False, donc opposer un veto."""
    return bool(re.search(r"\breturn\s+False\b", body))


def _has_safety_guard(body: str) -> bool:
    """Vrai si le veto épargne explicitement les sorties de sécurité.

    On exige que le motif de sortie soit RÉELLEMENT consulté : une stratégie qui
    ne regarde jamais `exit_reason` ne peut pas distinguer un stoploss d'un
    signal, quelle que soit la sophistication du reste.
    """
    if "exit_reason" not in body:
        return False
    return sum(k in body for k in SAFETY_KEYS) >= 3


# ── DETTE EXISTANTE, INVENTORIEE ────────────────────────────────────────────
# Ces stratégies sont DÉJÀ déployées et peuvent refuser une sortie de sécurité.
# Elles sont antérieures à la garde du générateur (2026-08-21). Les lister ici
# plutôt que de laisser le test rouge est un choix : un test qui échoue en
# permanence cesse d'être lu, et ne protège donc plus rien. Ce cliquet laisse
# passer la dette connue et fait échouer TOUTE NOUVELLE stratégie vulnérable.
#
# ⚠️ Cette liste ne doit que RÉTRÉCIR. N'y ajoutez jamais une entrée pour faire
# passer le test : corrigez la stratégie. Chaque nom ici est une position qui,
# le jour où elle passe sous l'eau, ne pourra pas être coupée autrement qu'en
# fermeture on-chain (incident coppock_keltner_short_v2, 2026-09-02).
DETTE_CONNUE: set[str] = set()
# ✅ VIDE, et c'est le but. Les 23 strategies deployees qui pouvaient refuser une
# sortie de securite ont ete corrigees le 2026-09-02. Cette liste n'existe plus que
# comme soupape : si une dette devait reapparaitre, elle serait NOMMEE ici plutot
# que de rendre le test rouge en permanence. Elle ne doit que retrecir, jamais
# grossir pour faire passer un test. Corrigez la strategie.


@pytest.mark.parametrize("path", list(_deployed_strategy_files()), ids=lambda p: p.name)
def test_une_sortie_de_securite_ne_peut_jamais_etre_refusee(path):
    if path.name in DETTE_CONNUE:
        pytest.xfail(f"dette connue : {path.name} (voir DETTE_CONNUE)")
    body = _confirm_trade_exit_body(path.read_text(encoding="utf-8", errors="ignore"))
    if body is None or not _can_deny(body):
        # Pas de veto possible : rien à garder.
        return
    assert _has_safety_guard(body), (
        f"{path.name} peut refuser une sortie sans consulter `exit_reason`.\n"
        "Un stoploss, un time-stop ou une fermeture externe seraient annules, et la "
        "position deviendrait incoupable (incident coppock_keltner_short_v2, 2026-09-02).\n"
        "Ajoute la garde du generateur : calculer `_is_safety_exit` a partir de "
        f"`exit_reason` sur {SAFETY_KEYS}, et ne jamais renvoyer False quand elle est vraie."
    )


def test_le_generateur_porte_la_meme_garde():
    """Le filet ne vaut que si la source des stratégies est saine elle aussi."""
    writer = (
        STRATEGY_DIR.parent / "strategies_generator" / "codegen" / "writer.py"
    )
    if not writer.exists():  # pragma: no cover - générateur absent chez un contributeur
        pytest.skip("générateur absent de cette installation")
    src = writer.read_text(encoding="utf-8", errors="ignore")
    assert "_is_safety_exit" in src, "le generateur a perdu sa garde de sortie de securite"
    for k in SAFETY_KEYS:
        assert k in src, f"le generateur ne protege plus le motif '{k}'"
