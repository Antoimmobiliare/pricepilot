# PricePilot

PricePilot analizza calendario e prenotazioni del tuo alloggio, applica regole di prezzo
esplicite e invia proposte da approvare. Il confronto con i competitor resta una verifica
manuale del proprietario: il percorso operativo non dipende dallo scraping.

Il progetto corrente è questa cartella dentro `ULTIMO PP`. Le modifiche di pre-lancio sono
state integrate qui preservando il database, il file `.env`, gli utenti e gli altri dati
già presenti. La vecchia cartella `PricePilot Prelaunch` non è la cartella operativa.

## Avvio locale

Fare doppio clic su `AVVIA_PRICEPILOT.cmd`, oppure eseguire da PowerShell:

```powershell
cd "C:\Users\UTENTE\OneDrive\Desktop\progetto Anto\ULTIMO PP\PricePilot_v4.1"
.\AVVIA_PRICEPILOT.cmd
```

Il launcher usa l’ambiente isolato `.venv-pp` e apre il servizio su
`http://127.0.0.1:8501`. Non ricrea né sostituisce il database.

I dettagli sono in [docs/AVVIO_LOCALE.md](docs/AVVIO_LOCALE.md). Lo stato e i limiti del
pre-lancio sono in [docs/PRELAUNCH_STATUS.md](docs/PRELAUNCH_STATUS.md); la logica di
pricing è descritta in [docs/CALENDAR_ONLY.md](docs/CALENDAR_ONLY.md).

## Flusso operativo

1. Beds24 acquisisce dalle OTA calendario e prenotazioni dell’appartamento.
2. PricePilot distingue notti libere, prenotate e indisponibili e calcola l’occupazione
   sulle notti vendibili.
3. Le regole per anticipo, occupazione, pickup, weekend e piccoli vuoti partono da una
   tariffa di riferimento stabile e rispettano minimo, massimo e soglia economica.
4. Telegram mostra data, prezzo corrente, proposta e motivazione.
5. Dopo l’approvazione PricePilot modifica solo lo slot prezzo Beds24 e lo rilegge.
6. La propagazione su Airbnb, Booking o Vrbo viene verificata separatamente.

La dashboard permette di configurare proprietà, policy e mapping Beds24 senza modificare
file JSON. Le credenziali restano segreti del server: nell’interfaccia si salvano soltanto
i nomi delle variabili che le contengono.

## Stato

Il codice di pre-lancio e i test con risposte controllate sono disponibili. Il sistema
non è ancora certificato live perché mancano account e annunci reali di Luma. Prima di
abilitare l’invio prezzi servono:

- account Beds24 e mapping reale di proprietà, alloggio e piano tariffario;
- token installati sul server e lettura completa riuscita;
- bot Telegram e collegamento all’appartamento;
- prova controllata di una tariffa, rilettura Beds24 e verifica su ogni OTA;
- deployment con scheduler e database condiviso collaudati.

`PRICEPILOT_ALLOW_CHANNEL_WRITES` deve restare `0` fino a quella prova. Nessun software può
garantire un aumento di occupazione o guadagno: le soglie iniziali vanno calibrate sui dati
reali di Luma e valutate attraverso ADR, RevPAR, margine e pickup.

## Verifica tecnica

I test usano database temporanei e credenziali disattivate:

```powershell
.\.venv-pp\Scripts\python.exe -m compileall -f pricepilot tests
.\.venv-pp\Scripts\python.exe scripts\run_tests.py
.\.venv-pp\Scripts\python.exe -m pip check
```

Per il cloud applicare gli schemi in `supabase/`, configurare Supabase Auth e mantenere
dashboard, API, webhook Telegram e scheduler sullo stesso archivio operativo. Gli ambienti
`staging`, `prod`, `production` e `live` rifiutano sessioni locali e autenticazione disabilitata.
