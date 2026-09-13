# PricePilot: stato pre-lancio

Aggiornamento: 11 settembre 2026.

Il lavoro di pre-lancio è stato integrato direttamente in `ULTIMO PP/PricePilot_v4.1`.
Il file `.env`, il database, gli utenti e gli altri dati operativi già presenti sono stati
preservati. `PricePilot Prelaunch` era la copia di lavoro e non è più la cartella da avviare.
PM OS resta fuori da questa fase.

## Stato del codice

Il percorso pre-lancio è implementato per funzionare sul calendario proprio dell’alloggio:

- calendario e prenotazioni Beds24 con distinzione tra libero, prenotato e indisponibile;
- occupazione calcolata sulle notti vendibili, senza dedurre prenotazioni dai blocchi;
- policy configurabile dall’interfaccia per tariffa di riferimento, anticipo, occupazione,
  weekend, pickup, date speciali e piccoli vuoti;
- revisione del soggiorno minimo come avviso manuale, senza scrittura automatica;
- limiti congiunti su minimo, massimo, soglia economica e variazione massima;
- proposta Telegram e nuova validazione di calendario, prezzo e policy all’approvazione;
- scrittura del solo prezzo Beds24 con rilettura, senza modificare disponibilità o restrizioni;
- snapshot, policy e mapping separati per account e appartamento;
- dashboard con dati persistiti; KPI economici mancanti restano “Non disponibile”;
- blocco dei dati simulati nel percorso operativo e autenticazione locale rifiutata negli
  ambienti `staging`, `prod`, `production` e `live`.

Il codice e i test controllati rendono la versione adatta alla configurazione prima degli
annunci. Non costituiscono una certificazione live di Beds24, Telegram o delle OTA.
L'ultima suite isolata ha superato 168 test su 168; il risultato riproducibile è salvato
in `docs/test-results.json`.

## Cosa può essere completato prima del lancio di Luma

- avvio e uso degli utenti esistenti;
- inserimento dei dati di Luma e dei limiti prezzo;
- definizione di una policy iniziale prudente, lasciandola disabilitata;
- preparazione dei nomi delle credenziali Beds24;
- verifica locale delle schermate, dello storico e dei blocchi di sicurezza.

## Gate live ancora necessari

Questi passaggi dipendono dagli account e dagli annunci reali e si completano al lancio:

1. Creare l’alloggio in Beds24 e collegare le OTA compatibili.
2. Salvare ID proprietà, ID alloggio e slot tariffario nella dashboard.
3. Installare token Beds24 sul server ed eseguire una lettura completa riuscita.
4. Collegare Telegram e verificare consegna, approvazione e rifiuto.
5. Provare una sola modifica tariffaria controllata, rileggerla in Beds24 e verificarla
   separatamente su Airbnb, Booking e Vrbo.
6. Abilitare `PRICEPILOT_ALLOW_CHANNEL_WRITES=1` soltanto dopo quella prova.
7. Verificare per più cicli lo scheduler ogni sei ore, i timeout e il recupero dagli errori.

La Home espone quattro stati distinti: configurazione, analisi calendario, proposte Telegram
e invio prezzi. L’ultimo resta “Da completare” fino a quando tutte le evidenze sopra sono presenti.

## Calibrazione economica

Le soglie iniziali sono regole del proprietario, non una previsione statistica e non una
garanzia di aumento dei risultati. Dopo le prime prenotazioni di Luma vanno confrontati:

- occupazione per finestra di anticipo;
- ADR e RevPAR calcolati dagli importi pernottamento classificati;
- nuove notti prenotate negli ultimi sette giorni;
- margine dopo commissioni e costi;
- frequenza con cui le proposte vengono approvate e risultato delle date modificate.

Se gli importi Beds24 includono pulizia o tasse e non possono essere separati, ADR, RevPAR e
ricavi restano non disponibili. PricePilot non sostituisce il dato con una stima.

## Avvio e verifica

Avviare `AVVIA_PRICEPILOT.cmd`, che usa `.venv-pp`. I test devono essere eseguiti senza
credenziali reali e con database temporanei:

```powershell
.\.venv-pp\Scripts\python.exe -m compileall -f pricepilot tests
.\.venv-pp\Scripts\python.exe scripts\run_tests.py
.\.venv-pp\Scripts\python.exe -m pip check
```

Dettagli della strategia: [CALENDAR_ONLY.md](CALENDAR_ONLY.md).
