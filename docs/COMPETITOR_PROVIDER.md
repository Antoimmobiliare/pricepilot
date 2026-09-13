# Dati competitor: componente facoltativo

Il percorso operativo scelto per PricePilot non interroga competitor e non dipende dallo
scraping. Il motore usa calendario, prenotazioni e tariffe del proprio alloggio; prima di
approvare su Telegram il proprietario può controllare manualmente gli annunci comparabili.

Nel codice resta un contratto separato per eventuali fonti di mercato future. Non è attivo
nel profilo `calendar_only` e non deve fornire valori inventati quando manca una fonte.
Un’eventuale integrazione futura dovrà usare un’API autorizzata o una raccolta permessa,
con provenienza, data di osservazione, condizioni del soggiorno, commissioni e imposte
esplicite. L’indisponibilità di una fonte non deve mai essere convertita in un prezzo.

Questa estensione non è necessaria per il lancio di Luma e non fa parte dei gate live
descritti in [`PRELAUNCH_STATUS.md`](PRELAUNCH_STATUS.md).
