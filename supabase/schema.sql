-- PricePilot Supabase schema
-- Esegui questo file nel SQL editor Supabase prima di attivare la sync reale.

create extension if not exists pgcrypto;

create table if not exists public.properties (
    id uuid primary key default gen_random_uuid(),
    account_id integer not null,
    local_id integer not null,
    name text not null,
    platform text not null default 'airbnb',
    listing_url text default '',
    listing_id text default '',
    city text default '',
    latitude double precision,
    longitude double precision,
    min_price numeric not null default 50,
    max_price numeric not null default 500,
    sync_mode text not null default 'advisory',
    strategy text not null default 'balanced',
    plan text not null default 'free',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(account_id, local_id)
);

create table if not exists public.pricing_rules (
    id uuid primary key default gen_random_uuid(),
    account_id integer not null,
    property_local_id integer not null,
    min_price numeric not null default 50,
    max_price numeric not null default 500,
    strategy text not null default 'balanced',
    sync_mode text not null default 'advisory',
    max_change_pct numeric,
    occupancy_low_threshold numeric,
    occupancy_high_threshold numeric,
    source text not null default 'pricepilot_dashboard',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(account_id, property_local_id)
);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists set_properties_updated_at on public.properties;
create trigger set_properties_updated_at
before update on public.properties
for each row execute function public.set_updated_at();

drop trigger if exists set_pricing_rules_updated_at on public.pricing_rules;
create trigger set_pricing_rules_updated_at
before update on public.pricing_rules
for each row execute function public.set_updated_at();

alter table public.properties enable row level security;
alter table public.pricing_rules enable row level security;

-- Policy permissive per la fase anon-key/serverless Streamlit.
-- Prima della produzione vera conviene passare a policy basate su auth.uid().
drop policy if exists "pricepilot_properties_anon_sync" on public.properties;
create policy "pricepilot_properties_anon_sync"
on public.properties
for all
to anon
using (true)
with check (true);

drop policy if exists "pricepilot_pricing_rules_anon_sync" on public.pricing_rules;
create policy "pricepilot_pricing_rules_anon_sync"
on public.pricing_rules
for all
to anon
using (true)
with check (true);
