-- Apply after schema.sql and cloud_primary_cutover.sql. No data replacement.
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
  primary key(account_id,property_id,kind)
);
create unique index if not exists uq_operational_documents_scope
  on public.operational_documents(account_id,property_id,kind);
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'operational_documents_property_scope_fk'
  ) then
    alter table public.operational_documents
      add constraint operational_documents_property_scope_fk
      foreign key(account_id,property_id)
      references public.properties(account_id,local_id)
      on delete cascade;
  end if;
end $$;
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'operational_documents_payload_scope_ck'
  ) then
    alter table public.operational_documents
      add constraint operational_documents_payload_scope_ck check (
        jsonb_typeof(payload) = 'object'
        and payload @> jsonb_build_object('account_id',account_id,'property_id',property_id)
      );
  end if;
end $$;
alter table public.operational_documents enable row level security;
revoke all on public.operational_documents from anon;
grant select,insert,update on public.operational_documents to authenticated;
grant all on public.operational_documents to service_role;
drop policy if exists operational_documents_read on public.operational_documents;
create policy operational_documents_read on public.operational_documents for select to authenticated
 using(private.is_account_member(account_id));
drop policy if exists operational_documents_write on public.operational_documents;
create policy operational_documents_write on public.operational_documents for all to authenticated
 using(exists(select 1 from public.account_members m where m.account_id=operational_documents.account_id and m.user_id=auth.uid() and m.role in ('owner','manager')))
 with check(exists(select 1 from public.account_members m where m.account_id=operational_documents.account_id and m.user_id=auth.uid() and m.role in ('owner','manager')));
commit;
