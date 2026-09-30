"""Account-scoped owner workflow. No credentials or invented market data in UI."""
from datetime import date, timedelta
import os

import pandas as pd
import streamlit as st

from pricepilot.core.database import get_account
from pricepilot.services.property_service import list_properties, get_property_by_id, create_property, update_property
from pricepilot.services.operational_store import (
    get_calendar_policy, save_calendar_policy, get_connection, save_connection,
)

_READ_CACHE_TTL_SECONDS = 12


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _properties(account_id):
    return list_properties(account_id=int(account_id))


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _account(account_id):
    return get_account(int(account_id))


def render_sidebar(account_id):
    with st.sidebar:
        st.markdown("## ✈️ PricePilot")
        st.caption("Calendario · Strategie · La tua approvazione")
        props = _properties(account_id)
        options = {p['id']: p['name'] for p in props}
        if options:
            current = st.session_state.get('active_prop_id')
            if current not in options:
                current = next(iter(options))
            chosen = st.selectbox('Appartamento', list(options),
                index=list(options).index(current), format_func=lambda v: options[v],
                key=f'operational_property_{account_id}')
            st.session_state['active_prop_id'] = chosen
        else:
            st.session_state['active_prop_id'] = None
            st.info('Aggiungi il primo appartamento in Proprietà.')
        st.divider()
        st.write('**Ogni modifica richiede il tuo consenso.**')
        st.caption('PP analizza il tuo calendario. Il confronto con i competitor resta una tua verifica prima di approvare.')
        st.caption('Preparazione annunci: puoi salvare le regole anche prima di collegare le OTA.')


def _property(account_id, property_id):
    return _cached_property(account_id, property_id) if property_id else None


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_property(account_id, property_id):
    return get_property_by_id(int(property_id), account_id=int(account_id))


def default_policy(reference=100):
    # Neutral defaults: opening a settings page never enables a discount.
    return {'enabled': False, 'reference_price': float(reference), 'break_even': 0,
            'minimum_change_eur': 2,
            'weekend_multiplier': 1, 'date_reference_prices': {},
            'gap_rule': {'enabled': False, 'max_nights': 2, 'through_days': 21,
                         'multiplier': .9},
            'pacing_rule': {'enabled': True, 'through_days': 60,
                            'low_pickup_7d_nights': 1, 'high_pickup_7d_nights': 5,
                            'low_multiplier': .95, 'high_multiplier': 1.05},
            'minimum_stay_rule': {'enabled': True, 'through_days': 21,
                                  'max_gap_nights': 2},
            'lead_time_bands': [
                {'through_days': 7, 'low_occupancy': .4, 'high_occupancy': .8, 'low_multiplier': 1, 'high_multiplier': 1},
                {'through_days': 30, 'low_occupancy': .3, 'high_occupancy': .7, 'low_multiplier': 1, 'high_multiplier': 1},
                {'through_days': 366, 'low_occupancy': .1, 'high_occupancy': .7, 'low_multiplier': 1, 'high_multiplier': 1},
            ]}


def render_properties(account_id, property_id=None):
    st.subheader('Appartamenti e regole')
    prop = _property(account_id, property_id)
    choices = ['Modifica appartamento selezionato', 'Aggiungi appartamento'] if prop else ['Aggiungi appartamento']
    action = st.radio('Operazione', choices, horizontal=True)
    editing = prop if action.startswith('Modifica') else {}
    with st.form(f'property_basics_{account_id}_{editing.get("id", "new")}'):
        name = st.text_input('Nome appartamento', value=editing.get('name', ''))
        city = st.text_input('Città e zona', value=editing.get('city', ''))
        details_left, details_right = st.columns(2)
        property_type = details_left.selectbox(
            'Tipologia', ['Appartamento intero', 'Stanza privata', 'Altro'],
            index=['Appartamento intero', 'Stanza privata', 'Altro'].index(
                {'entire_apartment': 'Appartamento intero', 'private_room': 'Stanza privata'}.get(
                    editing.get('property_type', ''), 'Altro')))
        max_guests = details_right.number_input('Ospiti massimi', min_value=1,
                                                value=int(editing.get('max_guests') or 1), step=1)
        area_m2 = details_left.number_input('Superficie (m², facoltativa)', min_value=0.0,
                                            value=float(editing.get('area_m2') or 0), step=1.0)
        layout_summary = details_right.text_input('Camere e posti letto (facoltativo)',
                                                  value=editing.get('layout_summary', ''))
        listing = st.text_input('Link annuncio (facoltativo prima del lancio)', value=editing.get('listing_url', ''))
        st.caption('Il link identifica l’annuncio. Calendario e prenotazioni arriveranno dal channel manager.')
        left, right = st.columns(2)
        minimum = left.number_input('Prezzo minimo per notte (€)', min_value=1.0, value=float(editing.get('min_price') or 50), step=5.0)
        maximum = right.number_input('Prezzo massimo per notte (€)', min_value=1.0, value=float(editing.get('max_price') or 300), step=5.0)
        if st.form_submit_button('Salva appartamento', type='primary'):
            if minimum >= maximum:
                st.error('Il minimo deve essere inferiore al massimo.')
            elif listing and not listing.startswith('https://'):
                st.error('Inserisci un link HTTPS oppure lascia il campo vuoto.')
            else:
                try:
                    account = _account(account_id) or {}
                    payload = {**editing, 'account_id': account_id, 'name': name.strip(), 'city': city.strip(),
                               'listing_url': listing.strip(), 'min_price': minimum, 'max_price': maximum,
                               'property_type': {'Appartamento intero': 'entire_apartment',
                                                 'Stanza privata': 'private_room', 'Altro': 'other'}[property_type],
                               'max_guests': int(max_guests),
                               'area_m2': float(area_m2) or None,
                               'layout_summary': layout_summary.strip(),
                               'sync_mode': 'approval', 'plan': account.get('plan', editing.get('plan', 'free'))}
                    saved = update_property(editing['id'], payload) if editing else create_property(payload)
                    st.session_state['active_prop_id'] = saved['id']
                    st.cache_data.clear()
                    st.success('Appartamento salvato. Configura le regole qui sotto.')
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if prop and action.startswith('Modifica'):
        render_policy(account_id, prop)


def render_policy(account_id, prop):
    st.markdown('### Strategie sul tuo calendario')
    st.caption('Le regole partono da una tariffa di riferimento stabile. Un nuovo ciclo non accumula sconti sul prezzo già scontato.')
    try:
        policy = get_calendar_policy(account_id, prop['id']) or default_policy(prop.get('min_price') or 100)
    except (RuntimeError, ValueError):
        st.error('Archivio delle regole non disponibile. Verifica la configurazione del database prima di salvare.')
        return
    with st.form(f'calendar_policy_{account_id}_{prop["id"]}'):
        a, b, c = st.columns(3)
        reference = a.number_input('Tariffa di riferimento (€)', min_value=1.0, max_value=100000.0,
                                   value=float(policy['reference_price']), step=5.0)
        break_even = b.number_input('Soglia economica minima per notte (€)', min_value=0.0, max_value=100000.0,
                                    value=float(policy.get('break_even', 0)), step=5.0)
        weekend = c.number_input('Variazione venerdì e sabato (%)', min_value=-50.0, max_value=100.0,
                                 value=round((float(policy.get('weekend_multiplier', 1))-1)*100, 2), step=1.0)
        minimum_change = st.number_input(
            'Ignora proposte inferiori a (€)', min_value=0.01, max_value=1000.0,
            value=float(policy.get('minimum_change_eur', 2)), step=1.0,
            help='Evita messaggi per differenze troppo piccole. Non modifica i limiti di sicurezza.',
        )
        st.write('**Finestre di anticipo e occupazione**')
        st.caption('Ogni riga vale dopo la precedente, fino al giorno indicato. L’ultima deve arrivare a 366. Occupazione: notti prenotate / notti vendibili nella finestra di 30 giorni dalla data analizzata. I blocchi non sono prenotazioni.')
        bands = [{'Fino a giorni': x['through_days'], 'Soglia bassa (%)': x['low_occupancy']*100,
                  'Soglia alta (%)': x['high_occupancy']*100, 'Variazione sotto soglia (%)': round((x['low_multiplier']-1)*100, 2),
                  'Variazione sopra soglia (%)': round((x['high_multiplier']-1)*100, 2)} for x in policy['lead_time_bands']]
        edited = st.data_editor(pd.DataFrame(bands), hide_index=True, num_rows='dynamic', width="stretch",
                               key=f'bands_{account_id}_{prop["id"]}')
        st.caption('Le soglie non sono una previsione della domanda. Imposta variazioni 0% per le finestre in cui vuoi solo osservare.')
        overrides = [{'Data': date.fromisoformat(day), 'Riferimento (€)': amount}
                     for day, amount in sorted(policy.get('date_reference_prices', {}).items())]
        dates = st.data_editor(pd.DataFrame(overrides, columns=['Data', 'Riferimento (€)']),
                    num_rows='dynamic', hide_index=True, width="stretch",
                    column_config={'Data': st.column_config.DateColumn('Data'),
                                   'Riferimento (€)': st.column_config.NumberColumn('Riferimento (€)', min_value=1)},
                    key=f'date_rates_{account_id}_{prop["id"]}')
        st.caption('Tariffe di riferimento per date particolari: stagionalità, festività o eventi che decidi tu. Nessuna fonte esterna automatica.')
        st.write('**Riempimento dei vuoti tra prenotazioni**')
        gap = policy.get('gap_rule') or {}
        gap_enabled = st.checkbox(
            'Applica una riduzione ai piccoli vuoti confermati',
            value=bool(gap.get('enabled', False)),
        )
        g1, g2, g3 = st.columns(3)
        gap_nights = g1.number_input('Vuoto massimo (notti)', min_value=1, max_value=7,
                                     value=int(gap.get('max_nights', 2)), step=1)
        gap_days = g2.number_input('Solo entro (giorni)', min_value=0, max_value=60,
                                   value=int(gap.get('through_days', 21)), step=1)
        gap_discount = g3.number_input('Riduzione vuoto (%)', min_value=0.0, max_value=50.0,
                                      value=round((1-float(gap.get('multiplier', .9)))*100, 2), step=1.0)
        st.caption('La regola scatta solo se il vuoto è delimitato da prenotazioni confermate e il soggiorno minimo consente di venderlo.')
        st.write('**Velocità delle prenotazioni**')
        pacing = policy.get('pacing_rule') or {}
        pacing_enabled = st.checkbox(
            'Usa le nuove notti prenotate negli ultimi 7 giorni',
            value=bool(pacing.get('enabled', True)),
        )
        p1, p2, p3 = st.columns(3)
        pacing_days = p1.number_input('Valuta pickup entro (giorni)', min_value=1, max_value=366,
                                      value=int(pacing.get('through_days', 60)), step=1)
        pickup_low = p2.number_input('Pickup basso fino a (notti)', min_value=0, max_value=29,
                                     value=int(pacing.get('low_pickup_7d_nights', 1)), step=1)
        pickup_high = p3.number_input('Pickup alto da (notti)', min_value=1, max_value=30,
                                      value=int(pacing.get('high_pickup_7d_nights', 5)), step=1)
        p4, p5 = st.columns(2)
        pickup_discount = p4.number_input('Variazione pickup basso (%)', min_value=-20.0, max_value=0.0,
                                          value=round((float(pacing.get('low_multiplier', .95))-1)*100, 2), step=1.0)
        pickup_increase = p5.number_input('Variazione pickup alto (%)', min_value=0.0, max_value=20.0,
                                          value=round((float(pacing.get('high_multiplier', 1.05))-1)*100, 2), step=1.0)
        st.caption('Il pickup è un dato del tuo alloggio, non una previsione di mercato. Se storico o data prenotazione sono incompleti, questa regola non viene usata.')
        st.write('**Revisione del soggiorno minimo**')
        minimum_stay = policy.get('minimum_stay_rule') or {}
        minimum_stay_enabled = st.checkbox(
            'Suggerisci una revisione quando il soggiorno minimo blocca un piccolo vuoto',
            value=bool(minimum_stay.get('enabled', True)),
        )
        m1, m2 = st.columns(2)
        minimum_stay_days = m1.number_input('Segnala entro (giorni)', min_value=0, max_value=60,
                                            value=int(minimum_stay.get('through_days', 21)), step=1)
        minimum_stay_gap = m2.number_input('Vuoto massimo da segnalare (notti)', min_value=1, max_value=7,
                                           value=int(minimum_stay.get('max_gap_nights', 2)), step=1)
        st.caption('PricePilot crea soltanto un avviso: non modifica automaticamente il soggiorno minimo su Beds24 o sulle OTA.')
        enabled = st.checkbox('Abilita queste regole per le proposte', value=bool(policy.get('enabled', False)))
        if st.form_submit_button('Salva regole', type='primary'):
            try:
                if not prop['min_price'] <= reference <= prop['max_price']:
                    raise ValueError('La tariffa di riferimento deve rientrare nei limiti dell’appartamento.')
                if break_even > prop['max_price']:
                    raise ValueError('La soglia economica non può superare il prezzo massimo.')
                if pickup_low >= pickup_high:
                    raise ValueError('La soglia pickup basso deve essere inferiore alla soglia pickup alto.')
                values = []
                for row in edited.to_dict('records'):
                    through = float(row['Fino a giorni'])
                    if not through.is_integer():
                        raise ValueError('I giorni devono essere numeri interi.')
                    values.append({'through_days': int(through), 'low_occupancy': float(row['Soglia bassa (%)'])/100,
                        'high_occupancy': float(row['Soglia alta (%)'])/100,
                        'low_multiplier': 1+float(row['Variazione sotto soglia (%)'])/100,
                        'high_multiplier': 1+float(row['Variazione sopra soglia (%)'])/100})
                specific = {}
                for row in dates.to_dict('records'):
                    day = row['Data']
                    if day is None or pd.isna(day):
                        raise ValueError('Completa o elimina le righe senza data.')
                    day = day.isoformat()[:10] if hasattr(day, 'isoformat') else str(day)
                    date.fromisoformat(day)
                    if day in specific:
                        raise ValueError('Una data compare più volte nelle tariffe particolari.')
                    amount = float(row['Riferimento (€)'])
                    if not prop['min_price'] <= amount <= prop['max_price']:
                        raise ValueError('Le tariffe particolari devono rientrare nei limiti dell’appartamento.')
                    specific[day] = amount
                save_calendar_policy(account_id, prop['id'], {**policy, 'enabled': enabled, 'reference_price': reference,
                    'break_even': break_even, 'weekend_multiplier': 1+weekend/100,
                    'minimum_change_eur': minimum_change,
                    'gap_rule': {'enabled': gap_enabled, 'max_nights': int(gap_nights),
                                 'through_days': int(gap_days), 'multiplier': 1-gap_discount/100},
                    'pacing_rule': {'enabled': pacing_enabled, 'through_days': int(pacing_days),
                                    'low_pickup_7d_nights': int(pickup_low),
                                    'high_pickup_7d_nights': int(pickup_high),
                                    'low_multiplier': 1+pickup_discount/100,
                                    'high_multiplier': 1+pickup_increase/100},
                    'minimum_stay_rule': {'enabled': minimum_stay_enabled,
                                          'through_days': int(minimum_stay_days),
                                          'max_gap_nights': int(minimum_stay_gap)},
                    'lead_time_bands': values, 'date_reference_prices': specific})
                st.cache_data.clear()
                st.success('Regole salvate. I collegamenti reali si configurano in Integrazioni.')
            except (ValueError, TypeError, KeyError) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else 'Completa tutte le celle con valori validi.')
            except RuntimeError:
                st.error('Salvataggio non riuscito. Verifica la disponibilità del database.')


def render_integrations(account_id, property_id=None):
    st.subheader('Calendario e prenotazioni')
    prop = _property(account_id, property_id)
    if not prop:
        st.info('Aggiungi o seleziona un appartamento in Proprietà.')
        return
    st.write('**Beds24 → PricePilot → Telegram → approvazione → Beds24**')
    st.caption('Beds24 raccoglie calendario e prenotazioni delle OTA. PP propone modifiche del solo prezzo; non riapre date occupate e non cambia le restrizioni di soggiorno.')
    try:
        mapping = get_connection(account_id, prop['id']) or {}
    except (ValueError, RuntimeError):
        st.error('Archivio collegamenti non disponibile. Verifica il database.')
        return
    with st.form(f'beds24_{account_id}_{prop["id"]}'):
        slot_verified = st.checkbox(
            'Ho verificato tecnicamente il piano tariffario Beds24 da usare',
            value=mapping.get('price_slot') is not None,
            help='Non selezionare un piano per tentativi: PricePilot lo usa per identificare il campo prezzo Beds24.',
        )
        a, b, c = st.columns(3)
        external = a.number_input('ID proprietà Beds24', min_value=0, value=int(mapping.get('beds24_property_id') or 0), step=1)
        room = b.number_input('ID alloggio Beds24', min_value=0, value=int(mapping.get('room_id') or 0), step=1)
        if slot_verified:
            slot = c.number_input('Piano tariffario Beds24 (1–16)', min_value=1, max_value=16,
                                  value=int(mapping.get('price_slot') or 1), step=1)
        else:
            slot = None
            c.caption('Piano tariffario: da verificare prima di salvarlo.')
        currency = st.selectbox('Valuta', ['EUR'], index=0, help='Luma Pisa opera in euro. La selezione resta esplicita nel mapping Beds24.')
        st.caption('Inseriremo gli ID dopo la creazione dell’alloggio in Beds24. Il mapping salva proprietà, alloggio, piano tariffario, valuta e soltanto i riferimenti alle variabili segrete.')
        confirmed_basis = st.checkbox('Ho verificato che l’importo prenotazione Beds24 contiene solo il pernottamento, senza pulizia e tasse',
                                       value=mapping.get('price_basis') == 'accommodation_only')
        st.caption('ADR e RevPAR restano non disponibili finché la composizione degli importi non è verificata.')
        enabled = st.checkbox('Abilita questo collegamento', value=bool(mapping.get('enabled', False)))
        with st.expander('Configurazione credenziali sul server'):
            st.caption('Questi campi indicano i nomi delle credenziali installate sul server. Non inserire qui chiavi API o password.')
            token_name = st.text_input('Nome variabile token Beds24', value=mapping.get('token_env', 'BEDS24_LUMA_TOKEN'))
            refresh_name = st.text_input('Nome variabile refresh token Beds24', value=mapping.get('refresh_token_env', 'BEDS24_LUMA_REFRESH_TOKEN'))
        if st.form_submit_button('Salva collegamento', type='primary'):
            try:
                save_connection(account_id, prop['id'], {'provider': 'beds24', 'enabled': enabled,
                    'beds24_property_id': int(external) or None, 'room_id': int(room) or None,
                    'price_slot': int(slot) if slot is not None else None, 'currency': currency,
                    'token_env': token_name.strip(), 'refresh_token_env': refresh_name.strip(),
                    'price_basis': 'accommodation_only' if confirmed_basis else 'unknown'})
                st.cache_data.clear()
                st.success('Configurazione salvata. Ora esegui la verifica di lettura.')
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except RuntimeError:
                st.error('Salvataggio non riuscito. Verifica il database.')
    credentials = bool(os.getenv(mapping.get('token_env', ''), '').strip()
                       or os.getenv(mapping.get('refresh_token_env', ''), '').strip())
    if mapping.get('enabled') and credentials:
        st.info('Configurazione presente. Verifica ora la lettura del calendario per questo appartamento.')
    else:
        st.info('Collegamento da completare al lancio: ID Beds24, credenziali sul server e attivazione.')
    if st.button('Verifica e aggiorna calendario', disabled=not (mapping.get('enabled') and credentials)):
        from pricepilot.services.beds24_sync import sync_property
        try:
            with st.spinner('Lettura calendario e prenotazioni…'):
                result = sync_property(account_id, prop['id'], date.today(), horizon_days=90)
            st.success(f"Calendario acquisito: {result['days']} giorni. Nessun prezzo inviato.")
        except Exception:
            st.error('Acquisizione non riuscita. Controlla ID, autorizzazioni e raggiungibilità di Beds24; non considerare aggiornati i dati precedenti.')
    st.caption('Il successo della lettura non certifica l’invio prezzi né la propagazione sulle OTA. Questi passaggi saranno collaudati con gli annunci reali.')
