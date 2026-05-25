"""
PricePilot - Pricing Strategies
Definizione delle strategie di pricing con relativi moltiplicatori e logiche.
"""
from dataclasses import dataclass
from typing import Dict


@dataclass
class Strategy:
    name: str
    label: str
    description: str
    base_multiplier: float      # moltiplicatore base sul prezzo calcolato
    competitor_weight: float    # quanto pesano i competitor (0-1)
    occupancy_sensitivity: float  # sensibilità all'occupancy (1 = normale)
    event_boost: float          # boost aggiuntivo in caso di evento
    min_margin: float           # margine minimo sul prezzo floor


STRATEGIES: Dict[str, Strategy] = {
    "conservative": Strategy(
        name="conservative",
        label="Conservativa",
        description="Prezzi stabili, variazioni minime. Ideale per chi vuole prevedibilità.",
        base_multiplier=1.00,
        competitor_weight=0.30,
        occupancy_sensitivity=0.70,
        event_boost=1.05,
        min_margin=0.05,
    ),
    "balanced": Strategy(
        name="balanced",
        label="Bilanciata",
        description="Equilibrio tra competitività e margine. Strategia raccomandata.",
        base_multiplier=1.05,
        competitor_weight=0.50,
        occupancy_sensitivity=1.00,
        event_boost=1.15,
        min_margin=0.10,
    ),
    "aggressive": Strategy(
        name="aggressive",
        label="Aggressiva",
        description="Massimizza occupancy con prezzi competitivi. Alta sensibilità al mercato.",
        base_multiplier=1.00,
        competitor_weight=0.70,
        occupancy_sensitivity=1.30,
        event_boost=1.20,
        min_margin=0.05,
    ),
    "premium": Strategy(
        name="premium",
        label="Premium",
        description="Posizionamento alto. Meno sensibile ai competitor, punta al RevPAR.",
        base_multiplier=1.25,
        competitor_weight=0.20,
        occupancy_sensitivity=0.60,
        event_boost=1.35,
        min_margin=0.20,
    ),
}


def get_strategy(name: str) -> Strategy:
    """Ritorna la strategia per nome, default 'balanced'."""
    return STRATEGIES.get(name, STRATEGIES["balanced"])
