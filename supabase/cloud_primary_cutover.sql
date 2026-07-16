-- PricePilot: passaggio a Supabase come database operativo unico.
--
-- Questo script e' idempotente e non cancella dati. Va eseguito DOPO
-- supabase/schema.sql. Completa lo schema per Streamlit, API Render, worker
-- Telegram e scheduler: tutti usano gli stessi record cloud.

create extension if not exists pgcrypto;

-- I record storici di PricePilot usano identificativi interi. Le tabelle
-- cloud hanno UUID come chiave tecnica; local_id mantiene compatibilita con
-- dashboard e motore senza introdurre un secondo database.
alter table public.profiles add column if not exists local_id bigint;
-- Alcuni account creati con lo schema iniziale avevano gia il trigger
-- set_profiles_updated_at ma non la colonna corrispondente.
alter table public.profiles
    add column if not exists updated_at timestamptz not null default now();

create sequence if not exists public.profiles_local_id_seq;
alter table public.profiles
    alter column local_id set default nextval('public.profiles_local_id_seq'::regclass);
update public.profiles
set local_id = nextval('public.profiles_local_id_seq'::regclass)
where local_id is null;
alter table public.profiles alter column local_id set not null;
create unique index if not exists uq_profiles_local_id on public.profiles(local_id);
select setval(
    'public.profiles_local_id_seq'::regclass,
    greatest(coalesce((select max(local_id) from public.profiles), 0), 1),
    true
);

do $$
declare
    table_name text;
    sequence_name text;
begin
    foreach table_name in array array[
        'properties',
        'guardrail_policies',
        'price_calendar',
        'decision_log',
        'occupancy_history',
        'market_history',
        'telegram_links',
        'telegram_approvals',
        'property_integrations',
        'operation_runs',
        'audit_events',
        'notification_preferences',
        'notification_log'
    ] loop
        sequence_name := 'pp_' || table_name || '_local_id_seq';
        execute format('create sequence if not exists public.%I', sequence_name);
        execute format('alter table public.%I add column if not exists local_id bigint', table_name);
        -- Alcuni progetti creati con versioni precedenti dello schema avevano
        -- gia il trigger set_updated_at, ma non ancora la relativa colonna.
        -- La aggiungiamo prima di qualsiasi UPDATE, cosi il passaggio resta
        -- idempotente anche in presenza di quegli schemi storici.
        execute format(
            'alter table public.%I add column if not exists updated_at timestamptz not null default now()',
            table_name
        );
        execute format(
            'alter table public.%I alter column local_id set default nextval(%L::regclass)',
            table_name,
            'public.' || sequence_name
        );
        execute format(
            'update public.%I set local_id = nextval(%L::regclass) where local_id is null',
            table_name,
            'public.' || sequence_name
        );
        execute format('alter table public.%I alter column local_id set not null', table_name);
        execute format(
            'create unique index if not exists %I on public.%I(account_id, local_id)',
            'uq_' || table_name || '_account_local_id',
            table_name
        );
        execute format(
            'select setval(%L::regclass, greatest(coalesce((select max(local_id) from public.%I), 0), 1), true)',
            'public.' || sequence_name,
            table_name
        );
    end loop;
end $$;

-- Le sequence sono necessarie quando una sessione autenticata crea una riga
-- senza specificare il local_id. Non danno accesso ai dati: quello resta
-- protetto dalle policy RLS sulle tabelle.
grant usage, select on all sequences in schema public to authenticated;

-- Sessione applicativa: hash di cookie firmati PricePilot. Non e' leggibile
-- dal client anonimo/autenticato; la usa soltanto il codice server-side.
create table if not exists public.app_sessions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    token_hash text not null unique,
    expires_at timestamptz not null,
    revoked_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists idx_app_sessions_token_hash on public.app_sessions(token_hash);
create index if not exists idx_app_sessions_user_id on public.app_sessions(user_id);
revoke all on table public.app_sessions from anon, authenticated;

-- Storico degli aggiornamenti applicati dal provider OTA/channel manager.
create sequence if not exists public.pp_price_updates_local_id_seq;
create table if not exists public.price_updates (
    id uuid primary key default gen_random_uuid(),
    account_id bigint not null,
    local_id bigint not null default nextval('public.pp_price_updates_local_id_seq'::regclass),
    property_local_id bigint not null,
    platform text not null default '',
    listing_id text not null default '',
    target_date date not null,
    new_price numeric,
    ok boolean not null default false,
    error text not null default '',
    applied_at timestamptz not null default now(),
    is_stub boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(account_id, local_id)
);
alter table public.price_updates
    add column if not exists updated_at timestamptz not null default now(),
    alter column local_id set default nextval('public.pp_price_updates_local_id_seq'::regclass);
create index if not exists idx_price_updates_property_time
    on public.price_updates(account_id, property_local_id, applied_at desc);

-- Tabelle analytics mantenute cloud-backed anche se i provider esterni non
-- sono ancora collegati. Inizialmente potranno contenere soli dati manuali.
create sequence if not exists public.pp_pricing_decisions_local_id_seq;
create table if not exists public.pricing_decisions (
    id uuid primary key default gen_random_uuid(),
    account_id bigint not null,
    local_id bigint not null default nextval('public.pp_pricing_decisions_local_id_seq'::regclass),
    timestamp timestamptz not null default now(),
    date date not null,
    property_id text not null default 'default',
    old_price numeric not null,
    new_price numeric not null,
    pct_change numeric not null default 0,
    competitor_price numeric,
    market_price numeric,
    competitor_count integer,
    competitor_min numeric,
    competitor_max numeric,
    occupancy numeric,
    event text not null default '',
    strategy text not null default 'balanced',
    decision text not null default '',
    applied boolean not null default false,
    created_at timestamptz not null default now(),
    unique(account_id, local_id)
);
create index if not exists idx_pricing_decisions_account_time
    on public.pricing_decisions(account_id, timestamp desc);

create table if not exists public.competitors (
    id uuid primary key default gen_random_uuid(),
    account_id bigint not null,
    timestamp timestamptz not null default now(),
    date date not null,
    source text not null default 'manual',
    property_name text not null default '',
    price numeric not null,
    occupancy_rate numeric,
    rating numeric,
    num_reviews integer,
    created_at timestamptz not null default now()
);
create index if not exists idx_competitors_account_date on public.competitors(account_id, date);

create table if not exists public.events (
    id uuid primary key default gen_random_uuid(),
    account_id bigint not null,
    date date not null,
    name text not null,
    event_type text not null default 'generic',
    impact_level text not null default 'medium',
    description text not null default '',
    created_at timestamptz not null default now(),
    unique(account_id, date, name)
);
create index if not exists idx_events_account_date on public.events(account_id, date);

create table if not exists public.market_snapshots (
    id uuid primary key default gen_random_uuid(),
    account_id bigint not null,
    timestamp timestamptz not null default now(),
    date date not null,
    market_avg numeric,
    market_min numeric,
    market_max numeric,
    competitor_count integer,
    our_price numeric,
    position text not null default '',
    created_at timestamptz not null default now()
);
create index if not exists idx_market_snapshots_account_date
    on public.market_snapshots(account_id, date desc);

-- RLS: l'app client puo accedere solo ai record del proprio account. Le
-- sessioni applicative restano server-only e non hanno policy utente.
alter table public.app_sessions enable row level security;
alter table public.price_updates enable row level security;
alter table public.pricing_decisions enable row level security;
alter table public.competitors enable row level security;
alter table public.events enable row level security;
alter table public.market_snapshots enable row level security;

drop policy if exists price_updates_member_all on public.price_updates;
create policy price_updates_member_all on public.price_updates
for all to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists pricing_decisions_member_all on public.pricing_decisions;
create policy pricing_decisions_member_all on public.pricing_decisions
for all to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists competitors_member_all on public.competitors;
create policy competitors_member_all on public.competitors
for all to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists events_member_all on public.events;
create policy events_member_all on public.events
for all to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists market_snapshots_member_all on public.market_snapshots;
create policy market_snapshots_member_all on public.market_snapshots
for all to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

-- Mantiene aggiornate le colonne timestamp delle tabelle create qui.
drop trigger if exists set_price_updates_created_at on public.price_updates;
create trigger set_price_updates_created_at
before update on public.price_updates
for each row execute function public.set_updated_at();

-- Controlli non distruttivi, utili dopo l'esecuzione.
select 'cloud_primary_cutover_ready' as status;
