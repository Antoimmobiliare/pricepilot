# PP: calendario proprio, strategie e approvazione Telegram

Decisione del proprietario, 10 settembre 2026: eliminare lo scraping dal flusso operativo.
PP propone modifiche dai dati del proprio alloggio; il confronto competitor resta manuale.
Questo documento sostituisce i precedenti requisiti che rendevano i dati competitor necessari.

## Flusso implementato

1. Acquisizione inventario e prenotazioni dal channel manager, senza contare i blocchi come prenotazioni.
2. Lettura del prezzo corrente della data e di una policy esplicita per account/immobile.
3. Calcolo dal prezzo di riferimento, con regole per anticipo, occupazione e weekend.
4. Controllo di minimo, massimo, costo minimo e variazione massima. Nessun provider competitor interrogato.
5. Proposta Telegram: data, prezzo attuale/proposto e spiegazione della regola. Nessuna media mercato fittizia.
6. Approvazione: verifica calendario, prezzo corrente, policy, validità temporale e ricalcolo prima dell'invio.
7. Conferma attraverso il channel manager. La propagazione OTA richiede un controllo separato.

La modalità auto viene limitata ad approval. Advisory resta disponibile per aggiornamento manuale.
Il prezzo di riferimento resta stabile quando cambia quello pubblicato: una regola del -10%
su 100 euro propone 90 euro, poi resta 90 se i dati e le regole non cambiano.
I limiti per variazione possono richiedere più passaggi approvati per raggiungere il riferimento.

## Configurazione dall'app

- In **Proprietà** si salvano, per l'appartamento selezionato, tariffa di riferimento,
  minimo economico, variazione minima, weekend, finestre di anticipo/occupazione,
  date speciali e regola per i piccoli vuoti tra prenotazioni.
- In **Integrazioni** si salvano gli ID Beds24, lo slot tariffario e i nomi delle
  variabili segrete. Token e password non vengono salvati nel database dell'app.
- Il pulsante **Verifica e aggiorna calendario** esegue una lettura reale senza inviare prezzi.
- La Home distingue quattro stati: configurazione, analisi calendario, proposte Telegram
  e invio prezzi. "Pronto" compare solo quando esistono le relative evidenze.
- I file JSON di esempio restano utili per fixture e migrazioni, ma non sono necessari
  nel normale flusso di configurazione via interfaccia.

## Regole operative

- Usare `.env.prelaunch.example` come riferimento per l'ambiente da configurare.
- Le finestre sono inclusive e ordinate per `through_days`; l'ultima deve coprire 366 giorni.
  `low_multiplier` si applica sotto `low_occupancy`, `high_multiplier` sopra `high_occupancy`;
  nella fascia intermedia il fattore è 1. Le soglie sono impostazioni, non stime di mercato.
- La tariffa di riferimento è per pernottamento; definire coerentemente occupazione standard,
  supplementi e mapping tariffa sul channel manager durante il setup.
- L'occupazione attuale del provider è la quota prenotata su notti vendibili nei 30 giorni
  a partire dalla data analizzata, escludendo blocchi. Non rappresenta un dato di mercato.
- Lasciare `PRICEPILOT_ALLOW_CHANNEL_WRITES=0` fino alla configurazione e prova dei collegamenti.
- Il riempimento vuoti si applica solo a un intervallo aperto delimitato da prenotazioni
  confermate e vendibile con il soggiorno minimo osservato. Se una di queste evidenze manca,
  non viene applicato lo sconto.
- Il pacing usa il numero lordo di notti oggi attive e create negli ultimi sette giorni.
  Le soglie iniziali suggerite sono 1 e 5 notti, entro 60 giorni, con fattori 0,95 e 1,05.
  Sono parametri del proprietario: non costituiscono una previsione e vanno ricalibrati sullo
  storico reale. Se le date di prenotazione sono incomplete, il fattore resta neutro.
- La revisione del soggiorno minimo è un avviso manuale entro 21 giorni per vuoti fino a due
  notti. PP non invia modifiche di soggiorno minimo al channel manager.

L'esempio è disabilitato e contiene valori dimostrativi. Nessuna regola è stata attivata su Luma.

## Funzionalità ancora da completare

Collaudo con account reali e deployment; verifica della propagazione Beds24 verso ogni OTA;
eventuale gestione diretta delle restrizioni di soggiorno; strategie calibrate sullo storico
reale di Luma; raggruppamento dei messaggi su più date. I test automatici attuali usano fixture
controllate e non costituiscono una prova live di Beds24, Telegram o OTA.
Non presentare queste funzionalità come già pronte né l'attuale sistema a regole come un
ottimizzatore statistico dei guadagni.
