# Avvio locale di PricePilot

## Avvio normale

1. Aprire `ULTIMO PP/PricePilot_v4.1`.
2. Fare doppio clic su `AVVIA_PRICEPILOT.cmd`.
3. Aprire `http://127.0.0.1:8501` se il browser non si apre automaticamente.
4. Lasciare aperta la finestra del server; `Ctrl+C` arresta l’app.

Il launcher usa `.venv-pp`. Il file `.env`, il database e gli utenti presenti nella cartella
non vengono copiati o ricreati.

## Se manca l’ambiente Python

Da PowerShell, nella cartella del progetto:

```powershell
py -3.12 -m venv .venv-pp
.\.venv-pp\Scripts\python.exe -m pip install -r requirements.lock.txt
```

La versione collaudata è Python 3.12 con le dipendenze bloccate in
`requirements.lock.txt`.

Non cancellare `.env` o i file database per risolvere problemi di avvio.

## Cosa è possibile provare prima degli annunci

- accesso con gli utenti locali esistenti in ambiente `development`;
- configurazione appartamento e regole calendario;
- configurazione del mapping Beds24 disabilitato;
- viste operative e storico già presente.

Calendario OTA, Telegram e invio prezzi restano incompleti finché non vengono installate le
credenziali e collegati gli account reali. Lasciare `PRICEPILOT_ALLOW_CHANNEL_WRITES=0`.

## Controlli rapidi

```powershell
.\.venv-pp\Scripts\python.exe -m pip check
.\.venv-pp\Scripts\python.exe -m compileall -f pricepilot tests
.\.venv-pp\Scripts\python.exe scripts\run_tests.py
```

L’app usa la porta 8501. Se è già occupata, arrestare la precedente istanza di PricePilot
prima di rilanciare il file `.cmd`.
