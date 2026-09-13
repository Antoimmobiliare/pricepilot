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
- `free_events`: festivita italiane offline e, con una chiave gratuita
  Ticketmaster, eventi pubblici vicini alla proprieta.
- `competitor_provider_unconfigured`: stato sicuro cloud quando non e ancora
  collegata una fonte competitor reale. Il piano Pro non entra in autopilot.

Impostare `PRICEPILOT_DATA_PROVIDER=manual` per usare:

- `data/manual_market.csv`;
- `data/manual_events.csv`;
- `data/manual_occupancy.csv`.

## Eventi automatici gratuiti

Impostare `PRICEPILOT_EVENT_PROVIDER=free`. Le festivita nazionali italiane
non richiedono chiavi. Per concerti, fiere, sport e festival pubblici impostare
anche `TICKETMASTER_API_KEY`, ottenibile dal programma Discovery API gratuito.

Il provider usa prima le coordinate della proprieta, quando presenti; in
alternativa usa la citta. I risultati sono memorizzati in cache durante il
ciclo per non interrogare inutilmente l'API.

## Competitor reali

I prezzi per notte dei competitor non vengono inventati in cloud. Un futuro
provider deve implementare `MarketDataProvider` e restituire competitor con
prezzo, distanza e statistiche normalizzate. Vedi
`docs/COMPETITOR_PROVIDER.md`.
