-- Persist operational identity fields for the first real property.
-- This stores only descriptive property metadata and does not change pricing logic.
begin;

alter table public.properties
    add column if not exists property_type text not null default '',
    add column if not exists max_guests integer,
    add column if not exists area_m2 numeric,
    add column if not exists layout_summary text not null default '';

commit;
