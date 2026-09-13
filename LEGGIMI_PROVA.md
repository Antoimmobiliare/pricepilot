# Avvio di PricePilot

`AVVIA_PROVA_LOCALE.cmd` è mantenuto soltanto come collegamento compatibile e avvia la
stessa applicazione di `AVVIA_PRICEPILOT.cmd` dentro `ULTIMO PP/PricePilot_v4.1`.

Questa è la cartella operativa originale aggiornata: usa il suo `.env`, il database e gli
utenti esistenti. Per l’avvio e i controlli consultare
[`docs/AVVIO_LOCALE.md`](docs/AVVIO_LOCALE.md).

Prima del collegamento degli annunci, lasciare disabilitati sia il mapping Beds24 sia
`PRICEPILOT_ALLOW_CHANNEL_WRITES`. La modalità isolata usata dai test automatici non è
il launcher dell’app e usa sempre un database temporaneo.
