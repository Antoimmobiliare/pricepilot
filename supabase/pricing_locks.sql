-- Apply after schema.sql/cloud_primary_cutover.sql. Ephemeral coordination only.
begin;
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
  delete from public.pricing_date_locks where account_id=p_account_id and property_id=p_property_id
    and target_date=p_target_date and owner_token=p_owner_token;
  get diagnostics changed = row_count;
  return changed = 1;
end $$;
revoke all on function public.acquire_pricepilot_pricing_lock(bigint,bigint,date,text,timestamptz) from public,anon,authenticated;
revoke all on function public.release_pricepilot_pricing_lock(bigint,bigint,date,text) from public,anon,authenticated;
grant execute on function public.acquire_pricepilot_pricing_lock(bigint,bigint,date,text,timestamptz) to service_role;
grant execute on function public.release_pricepilot_pricing_lock(bigint,bigint,date,text) to service_role;
commit;
