# PricePilot Providers

Questo pacchetto contiene i contratti delle integrazioni esterne.

- `MarketDataProvider`: prezzi competitor, media mercato, statistiche.
- `EventProvider`: eventi locali rilevanti per la data.
- `OccupancyProvider`: occupazione reale o stimata.
- `ChannelManagerProvider`: applicazione prezzi su OTA/channel manager.
- `BillingProvider`: piano attivo, stato abbonamento e permessi.

Il motore usa solo questi contratti. Quando colleghiamo API reali, registriamo
un provider nuovo nel registry senza cambiare la logica di pricing.

Provider disponibili oggi:

- `demo`: dati simulati per sviluppo rapido.
- `manual`: CSV locali per test realistici gratuiti prima di comprare API.

Impostare `PRICEPILOT_DATA_PROVIDER=manual` per usare:

- `data/manual_market.csv`;
- `data/manual_events.csv`;
- `data/manual_occupancy.csv`.
