-- PricePilot pre-lancio: eseguire una sola volta nel Supabase SQL Editor.
-- Migrazione idempotente: crea solo archivio operativo e lock, senza sostituire dati.
begin;

create table if not exists public.operational_documents (
  account_id bigint not null references public.accounts(id),
  property_id bigint not null,
  kind text not null check (kind in ('calendar_policy','connection','snapshot')),
  payload jsonb not null check (
    jsonb_typeof(payload) = 'object'
    and payload @> jsonb_build_object('account_id',account_id,'property_id',property_id)
  ),
  updated_at timestamptz not null default now(),
  primary key(account_id,property_id,kind),
  foreign key(account_id,property_id)
    references public.properties(account_id,local_id) on delete cascade
);
alter table public.operational_documents enable row level security;
revoke all on public.operational_documents from anon;
grant select,insert,update on public.operational_documents to authenticated;
grant all on public.operational_documents to service_role;
drop policy if exists operational_documents_read on public.operational_documents;
create policy operational_documents_read on public.operational_documents for select to authenticated
  using(private.is_account_member(account_id));
drop policy if exists operational_documents_write on public.operational_documents;
create policy operational_documents_write on public.operational_documents for all to authenticated
  using(exists(
    select 1 from public.account_members m
    where m.account_id=operational_documents.account_id
      and m.user_id=auth.uid() and m.role in ('owner','manager')
  ))
  with check(exists(
    select 1 from public.account_members m
    where m.account_id=operational_documents.account_id
      and m.user_id=auth.uid() and m.role in ('owner','manager')
  ));

create table if not exists public.pricing_date_locks (
  account_id bigint not null references public.accounts(id) on delete cascade,
  property_id bigint not null,
  target_date date not null,
  owner_token text not null,
  expires_at timestamptz not null,
  primary key(account_id, property_id, target_date),
  foreign key(account_id, property_id)
    references public.properties(account_id, local_id) on delete cascade
);
alter table public.pricing_date_locks enable row level security;
revoke all on public.pricing_date_locks from anon, authenticated;
grant all on public.pricing_date_locks to service_role;

create or replace function public.acquire_pricepilot_pricing_lock(
  p_account_id bigint, p_property_id bigint, p_target_date date,
  p_owner_token text, p_expires_at timestamptz
) returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
declare changed integer;
begin
  if p_expires_at <= now() or p_expires_at > now() + interval '30 minutes' then
    raise exception 'invalid lock expiry';
  end if;
  insert into public.pricing_date_locks(account_id,property_id,target_date,owner_token,expires_at)
  values(p_account_id,p_property_id,p_target_date,p_owner_token,p_expires_at)
  on conflict(account_id,property_id,target_date) do update
    set owner_token=excluded.owner_token, expires_at=excluded.expires_at
    where public.pricing_date_locks.expires_at < now();
  get diagnostics changed = row_count;
  return changed = 1;
end $$;

create or replace function public.release_pricepilot_pricing_lock(
  p_account_id bigint, p_property_id bigint, p_target_date date, p_owner_token text
) returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
declare changed integer;
begin
  delete from public.pricing_date_locks
  where account_id=p_account_id and property_id=p_property_id
    and target_date=p_target_date and owner_token=p_owner_token;
  get diagnostics changed = row_count;
  return changed = 1;
end $$;
revoke all on function public.acquire_pricepilot_pricing_lock(bigint,bigint,date,text,timestamptz)
  from public,anon,authenticated;
revoke all on function public.release_pricepilot_pricing_lock(bigint,bigint,date,text)
  from public,anon,authenticated;
grant execute on function public.acquire_pricepilot_pricing_lock(bigint,bigint,date,text,timestamptz)
  to service_role;
grant execute on function public.release_pricepilot_pricing_lock(bigint,bigint,date,text)
  to service_role;

commit;

select
  to_regclass('public.operational_documents') is not null as operational_documents_ready,
  to_regclass('public.pricing_date_locks') is not null as pricing_date_locks_ready,
  to_regprocedure('public.acquire_pricepilot_pricing_lock(bigint,bigint,date,text,timestamptz)') is not null
    as acquire_lock_ready,
  to_regprocedure('public.release_pricepilot_pricing_lock(bigint,bigint,date,text)') is not null
    as release_lock_ready;
