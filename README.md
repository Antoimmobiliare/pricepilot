# ✈️ PricePilot — Dynamic Pricing per Affitti Brevi

**PricePilot** è una piattaforma di dynamic pricing per Airbnb, Booking.com e altri canali di affitto breve. Analizza i prezzi dei competitor, considera eventi locali e occupancy, e suggerisce il prezzo ottimale per massimizzare il revenue.

---

## 🚀 Quickstart

```bash
# 1. Installa dipendenze
pip install -r requirements.txt

# 2. Popola il database con 90 giorni di dati demo
python seed_demo.py

# 3. Avvia la dashboard
streamlit run pricepilot/dashboard/app.py
```

La dashboard si aprirà su `http://localhost:8501`

---

## 📁 Struttura Progetto

```
pricepilot/
├── core/
│   ├── config.py         # Configurazione (JSON persistente)
│   ├── database.py       # SQLite: schema + CRUD
│   ├── data_loader.py    # Caricamento CSV
│   └── scheduler.py      # Loop periodico
├── pricing/
│   ├── engine.py         # Motore di pricing principale
│   ├── strategies.py     # Strategie (conservative/balanced/aggressive/premium)
│   └── safety.py         # Floor, ceiling, max-change rules
├── data_sources/
│   ├── competitors.py    # Dati competitor (simulati + estendibili)
│   └── events.py         # Catalogo eventi locali
├── analytics/
│   └── analyzer.py       # KPI, serie temporali, correlazioni
├── notifications/
│   └── notifier.py       # Console + Telegram
├── export/
│   └── exporter.py       # Export CSV/JSON
├── dashboard/
│   └── app.py            # Dashboard Streamlit
├── data/
│   ├── sample_data.csv   # Dati campione
│   ├── config.json       # Configurazione (auto-generato)
│   └── pricepilot.db     # Database SQLite (auto-generato)
├── main.py               # CLI entry point
└── seed_demo.py          # Script seed dati demo
```

---

## 🎯 Strategie di Pricing

| Strategia    | Descrizione                                        |
|--------------|----------------------------------------------------|
| Conservative | Prezzi stabili, variazioni minime                  |
| Balanced     | Equilibrio tra competitività e margine *(default)* |
| Aggressive   | Massimizza occupancy, segue il mercato             |
| Premium      | Posizionamento alto, punta al RevPAR               |

---

## 💻 CLI

```bash
# Un solo ciclo (oggi)
python -m pricepilot.main --once

# Data specifica
python -m pricepilot.main --date 2025-12-24

# Loop continuo (ogni 6h)
python -m pricepilot.main --loop

# Popola dati demo
python -m pricepilot.main --seed --days 90
```

---

## 🔔 Notifiche Telegram

1. Crea un bot con @BotFather su Telegram
2. Ottieni il token e il chat_id
3. Inserisci nella dashboard (sidebar → Notifiche Telegram) o in `data/config.json`

---

## 🗄️ Schema Database SQLite

| Tabella             | Contenuto                              |
|---------------------|----------------------------------------|
| `pricing_decisions` | Storico completo di ogni decisione     |
| `competitors`       | Snapshot prezzi competitor             |
| `events`            | Catalogo eventi locali                 |
| `market_snapshots`  | Statistiche di mercato giornaliere     |

---

## 🔮 Roadmap

- [ ] Integrazione AirDNA API (dati competitor reali)
- [ ] Connettore Airbnb iCal per occupancy reale
- [ ] Multi-property support
- [ ] Machine Learning pricing (XGBoost/LightGBM)
- [ ] Report PDF automatici
- [ ] App mobile

---

*PricePilot v1.0 — Built with Python + Streamlit + SQLite*
