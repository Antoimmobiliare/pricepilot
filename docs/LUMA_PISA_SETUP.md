# Configurazione iniziale di Luma Pisa

Questa procedura prepara PricePilot a lavorare con il calendario reale senza inviare ancora modifiche a Beds24. Il flusso attivo e' sempre: analisi, proposta Telegram, approvazione esplicita.

## 1. Apri l'app pulita

Accedi a PricePilot con l'account che userai per Luma Pisa. Se il database operativo e' stato azzerato, il primo accesso crea un account applicativo vuoto: e' previsto. Apri **Proprieta'** e scegli **Crea la proprieta'**.

Inserisci almeno:

- Nome: `Luma Pisa`.
- Citta': `Pisa`.
- Tipo: appartamento intero.
- Capienza: 4 ospiti, con camera matrimoniale e divano letto.
- I limiti iniziali scelti da te: tariffa di riferimento, floor, ceiling e variazione massima. I limiti possono essere aggiornati in seguito senza ricreare la proprieta'.

Lascia la sincronizzazione su **Approvazione**. Nell'ambiente operativo PricePilot la mantiene comunque in approvazione e usa le regole equivalenti al profilo Plus; non esiste un'opzione di automazione completa.

## 2. Configura Beds24, senza inserire segreti nell'app

Quando Luma sara' presente in Beds24, apri **Integrazioni > Beds24** per Luma Pisa e inserisci:

- `beds24_property_id`: identificativo della proprieta' in Beds24.
- `room_id`: identificativo della camera/unita' vendibile.
- `price_slot`: lo slot tariffario Beds24 da leggere e, solo in futuro, da aggiornare.
- Valuta: `EUR`.
- Nome variabile token: `BEDS24_LUMA_TOKEN`.
- Nome variabile refresh token: `BEDS24_LUMA_REFRESH_TOKEN`, se previsto dal token Beds24.

I due ultimi campi sono soltanto riferimenti. Il valore reale del token deve essere inserito solo nei Secret del servizio che esegue PricePilot (Render e, se necessario, Streamlit Cloud), mai nella dashboard, nel database, in Git o nei file del repository.

## 3. Mantieni il collaudo in sola lettura

Nei Secret di Render devono restare questi valori:

```text
PRICEPILOT_OPERATIONAL_MODE=1
PRICEPILOT_ALLOW_CHANNEL_WRITES=0
PRICEPILOT_PRICING_BASIS=calendar_only
PRICEPILOT_CHANNEL_PROVIDER=beds24
PRICEPILOT_OCCUPANCY_PROVIDER=observed_inventory
```

Poi inserisci i token Beds24 nei Secret di Render con i nomi indicati al punto 2. Con `PRICEPILOT_ALLOW_CHANNEL_WRITES=0`, PricePilot puo' leggere calendario, disponibilita', prenotazioni e tariffe dal collegamento configurato, ma rifiuta ogni scrittura di prezzo anche dopo un'approvazione Telegram.

## 4. Verifica prima dell'apertura degli annunci

1. Collega il bot Telegram dall'area Telegram dell'app e verifica il messaggio di test.
2. Esegui una lettura Beds24 e controlla che date, notti occupate, tariffe e valuta coincidano con Beds24.
3. Genera una proposta di prova: deve mostrare prezzo attuale, prezzo proposto, motivazione e guardrail applicati.
4. Approvala una sola volta e verifica il read-back/audit: con le scritture disabilitate non deve cambiare alcun prezzo in Beds24.
5. Solo quando le OTA e il sito diretto sono sincronizzati tramite Beds24 e la prova e' corretta, si potra' valutare separatamente l'abilitazione delle scritture approvate.

Le protezioni restano attive durante tutto il flusso: floor e ceiling, variazione massima, break-even/dynamic floor, lock delle date, verifica della vendibilita', read-back Beds24, idempotenza dell'approvazione e isolamento fra account e proprieta'.
