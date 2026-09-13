# Channel manager per Luma

Valutazione del 10 settembre 2026: **Beds24 è il candidato principale** per costo modulare,
API documentate e crescita a più appartamenti. Il connettore preparato nella copia pre-lancio
usa API V2; non costituisce una connessione attiva o una certificazione dell'integrazione.

Il listino parte da 15,50 euro/mese, con voci aggiuntive legate alla configurazione,
inclusi collegamenti ai canali. Il prezzo effettivo di Luma va calcolato con appartamenti,
unità e canali scelti: il prezzo base non è un preventivo complessivo.
[Listino ufficiale Beds24](https://www.beds24.com/pricing.html).

La configurazione richiede attenzione: ID immobile/camera, tariffa da aggiornare,
restrizioni e collegamenti OTA devono essere verificati. Per Vrbo va confermata
l'idoneità dell'account e la procedura applicabile a Luma prima di sottoscrivere un servizio
per questa finalità. [Procedura Beds24 per Vrbo](https://wiki.beds24.com/index.php/VRBO_Setup).

Smoobu resta un'alternativa da valutare: listino mensile pubblicato di 35 euro per un
appartamento e 12 euro per appartamento aggiuntivo, IVA esclusa. La modalità del collegamento
Vrbo va verificata: una connessione iCal sincronizza disponibilità, non invia prezzi.
[Listino Smoobu](https://support.smoobu.com/hc/en-us/articles/360003170680-How-much-does-Smoobu-cost-Plans-and-pricing-explained),
[collegamenti canali Smoobu](https://support.smoobu.com/hc/en-us/articles/38213700242066-Connect-Vrbo-HomeToGo-Expedia-and-other-channels-to-Smoobu).

Il channel manager gestisce tariffe e prenotazioni dei propri alloggi. Con il requisito
aggiornato, PP usa questi dati per proporre prezzi e il proprietario confronta manualmente
i competitor prima di approvare. **Non serve un abbonamento a una fonte dati competitor.**

Riferimento tecnico del connettore:
[API Beds24 V2](https://beds24.com/api/v2/) e schema salvato in `beds24-apiV2.yaml`.
