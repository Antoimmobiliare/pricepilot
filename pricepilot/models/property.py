"""
PricePilot - Property Model
Dataclass per rappresentare una proprietà in modo tipizzato.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


SYNC_MODES = ("advisory", "approval", "auto")
PLATFORMS  = ("airbnb", "booking", "vrbo", "direct", "other")
PLANS      = ("free", "plus", "pro")


@dataclass
class Property:
    name:        str
    platform:    str         = "airbnb"
    listing_url: str         = ""
    listing_id:  str         = ""
    city:        str         = ""
    latitude:    Optional[float] = None
    longitude:   Optional[float] = None
    min_price:   float       = 50.0
    max_price:   float       = 500.0
    sync_mode:   str         = "advisory"
    plan:        str         = "free"
    account_id:  int         = 1
    id:          Optional[int]  = None
    created_at:  str         = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at:  str         = field(default_factory=lambda: datetime.utcnow().isoformat())

    def __post_init__(self):
        if self.sync_mode not in SYNC_MODES:
            raise ValueError(f"sync_mode deve essere uno di: {SYNC_MODES}")
        if self.plan not in PLANS:
            self.plan = "free"
        if self.platform not in PLATFORMS:
            self.platform = "other"
        if self.min_price >= self.max_price:
            raise ValueError("min_price deve essere < max_price")

    @property
    def base_price(self) -> float:
        """Prezzo medio tra min e max."""
        return round((self.min_price + self.max_price) / 2, 2)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Property":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered   = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)
