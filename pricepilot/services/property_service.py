"""
PricePilot - Property Service
Logica di business per la gestione proprietà.
Fa da intermediario tra la dashboard/API e il database.
"""
import logging
from typing import List, Optional, Dict

from pricepilot.core.database import (
    upsert_property, get_properties, get_property, delete_property, get_account,
)
from pricepilot.models.property import Property, SYNC_MODES, PLATFORMS
from pricepilot.core.plans import PLANS, effective_sync_mode, get_plan_limit, normalize_plan

logger = logging.getLogger("pricepilot.property_service")


def list_properties(account_id: Optional[int] = None) -> List[Dict]:
    """Ritorna le proprieta dell'account, o tutte in modalita admin/dev."""
    props = get_properties()
    if account_id is None:
        return props
    return [p for p in props if int(p.get("account_id") or 1) == int(account_id)]


def get_property_by_id(prop_id: int, account_id: Optional[int] = None) -> Optional[Dict]:
    """Ritorna una proprieta per id, rispettando l'account se passato."""
    prop = get_property(prop_id)
    if not prop:
        return None
    if account_id is not None and int(prop.get("account_id") or 1) != int(account_id):
        return None
    return prop


def create_property(data: Dict) -> Dict:
    """
    Crea una nuova proprietà con validazione.
    Ritorna la proprietà creata con il suo id.
    """
    _validate(data)
    _enforce_property_limit(data)
    new_id = upsert_property(data)
    prop   = get_property(new_id)
    logger.info(f"Proprietà creata: id={new_id} name={data['name']}")
    return prop


def update_property(prop_id: int, data: Dict) -> Optional[Dict]:
    """Aggiorna una proprietà esistente."""
    existing = get_property(prop_id)
    if not existing:
        return None
    merged = {**existing, **data, "id": prop_id}
    _validate(merged)
    upsert_property(merged)
    logger.info(f"Proprietà aggiornata: id={prop_id}")
    return get_property(prop_id)


def remove_property(prop_id: int) -> bool:
    """Rimuove una proprietà. Ritorna True se esisteva."""
    if not get_property(prop_id):
        return False
    delete_property(prop_id)
    logger.info(f"Proprietà eliminata: id={prop_id}")
    return True


def get_or_create_default(account_id: int = 1) -> Dict:
    """
    Ritorna la prima proprietà disponibile o ne crea una di default.
    Utile per la dashboard single-property.
    """
    props = list_properties(account_id=account_id)
    if props:
        return props[0]
    default = {
        "account_id":   account_id,
        "name":        "La mia proprieta",
        "platform":    "airbnb",
        "listing_url": "",
        "listing_id":  "",
        "city":        "Italia",
        "min_price":   50.0,
        "max_price":   300.0,
        "plan":        "free",
        "sync_mode":   "advisory",
    }
    return create_property(default)


def _validate(data: Dict) -> None:
    if not data.get("name", "").strip():
        raise ValueError("Il nome della proprietà è obbligatorio.")
    if data.get("sync_mode") and data["sync_mode"] not in SYNC_MODES:
        raise ValueError(f"sync_mode deve essere uno di: {SYNC_MODES}")
    data["plan"] = normalize_plan(data.get("plan"))
    if data["plan"] not in PLANS:
        raise ValueError(f"plan deve essere uno di: {PLANS}")
    data["sync_mode"] = effective_sync_mode(data["plan"], data.get("sync_mode"))
    min_p = float(data.get("min_price", 0))
    max_p = float(data.get("max_price", 0))
    if min_p >= max_p:
        raise ValueError("min_price deve essere inferiore a max_price.")


def _enforce_property_limit(data: Dict) -> None:
    account_id = int(data.get("account_id") or 1)
    account = get_account(account_id) or {"plan": data.get("plan", "free")}
    plan = account.get("plan") or data.get("plan", "free")
    max_properties = int(get_plan_limit(plan, "max_properties", 1))
    current_count = sum(
        1 for p in get_properties()
        if int(p.get("account_id") or 1) == account_id
    )
    if current_count >= max_properties:
        raise ValueError(
            f"Il piano {plan} permette massimo {max_properties} proprieta. "
            "Aggiorna il piano per aggiungerne altre."
        )
