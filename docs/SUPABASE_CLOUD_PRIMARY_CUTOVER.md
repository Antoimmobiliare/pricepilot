# Cutover: Supabase database operativo unico

Questo passaggio fa usare a PricePilot una sola sorgente dati condivisa da
dashboard Streamlit, FastAPI su Render, webhook Telegram e futuri scheduler.
SQLite resta disponibile soltanto per sviluppo locale e test: non viene piu
consultato dai processi cloud.

## Cosa serve

- il progetto Supabase gia creato, con `supabase/schema.sql` eseguito;
- una sessione utente PricePilot gia registrata in Supabase;
- l'account cloud di destinazione. Nel Table Editor e nella dashboard e il
  valore numerico `account_id`, non l'ID UUID dell'utente;
- la `SUPABASE_SERVICE_ROLE_KEY`, conservata solo nei secret server-side.

Non inserire mai la service role nel repository, nel browser, nelle variabili
pubbliche o in un messaggio Telegram.

## 1. Completa lo schema cloud

In **Supabase > SQL Editor**, apri il file locale
`supabase/cloud_primary_cutover.sql`, incollalo in una nuova query ed eseguilo.
E' idempotente e additivo: non elimina account, proprieta o storico esistenti.
L'ultimo risultato deve contenere `cloud_primary_cutover_ready`.

Lo script aggiunge gli identificativi compatibili con il motore storico,
sessioni applicative server-side, storico aggiornamenti e tabelle analytics
mancanti. Le nuove tabelle sono protette da RLS per `account_id`.

## 2. Verifica prima senza scrivere

Nel PC che contiene ancora il database SQLite, lascia:

```env
PRICEPILOT_DATABASE_BACKEND=sqlite
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

Poi esegui dal terminale del progetto, sostituendo i due ID. Di solito
`source-account-id` e' `1`; `cloud-account-id` e' quello dell'account creato
dal tuo login Supabase.

```powershell
.\.venv\Scripts\python.exe -m pricepilot.services.supabase_migration `
  --source-account-id 1 --cloud-account-id 11
```

Questo e' un dry-run: conta proprieta, decisioni, calendario, collegamenti
Telegram e storico ma non modifica Supabase.

## 3. Esegui la migrazione una volta

Se i conteggi sono corretti, aggiungi `--execute`:

```powershell
.\.venv\Scripts\python.exe -m pricepilot.services.supabase_migration `
  --source-account-id 1 --cloud-account-id 11 --execute
```

Puoi rieseguirla senza duplicare i dati: tutte le scritture usano chiavi
univoche per account e identificativo locale. Il risultato deve avere
`"ok": true`. Se una tabella mostra un errore, non attivare ancora il backend
cloud: correggi quel punto e ripeti lo stesso comando.

## 4. Verifica i record in Supabase

Nel Table Editor controlla, filtrando per il tuo `account_id`:

- `properties`;
- `pricing_rules`;
- `price_calendar` e `decision_log`, se esistono dati storici;
- `telegram_links` e `telegram_approvals`, se il bot era gia collegato;
- `operation_runs`, `audit_events`, `notification_log`.

L'ID visibile al motore PricePilot e' `local_id`; l'UUID e' la chiave tecnica
cloud. Non modificare manualmente i `local_id`.

## 5. Attiva il backend cloud, servizio per servizio

Inserisci **in entrambi** i pannelli secret (Streamlit Community Cloud e
Render), mai nel file Git:

```env
PRICEPILOT_DATABASE_BACKEND=supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

Mantieni le altre variabili esistenti (URL dell'app, Telegram, API key e
webhook). Salva e redeploy prima Render, poi Streamlit.

Per il worker Telegram locale, se lo usi ancora in polling, aggiungi le stesse
quattro variabili al suo `.env`. Con webhook Render non serve tenere acceso il
PC.

## 6. Collaudo finale

1. Accedi a Streamlit e modifica min/max di una proprieta.
2. Ricarica la dashboard: il valore deve restare identico.
3. Apri la stessa dashboard da un altro browser/dispositivo: deve mostrare lo
   stesso account e gli stessi valori.
4. Genera un ciclo di test e verifica in Supabase una nuova riga in
   `operation_runs` e `decision_log`.
5. Invia una proposta Telegram e approva/rifiuta: controlla
   `telegram_approvals` e lo stato della decisione.
6. Visita `https://<api-render>/ready`: Supabase deve risultare configurato.

## Ritorno temporaneo allo sviluppo locale

Per lavorare senza cloud, imposta solo sul PC:

```env
PRICEPILOT_DATABASE_BACKEND=sqlite
```

Non usare questa impostazione nei secret di Streamlit o Render dopo il
cutover: riporterebbe i servizi a database separati.
