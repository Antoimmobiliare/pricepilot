-- PricePilot Supabase security advisor hardening
-- Run this in the Supabase SQL editor after the main schema has already been applied.
--
-- What it fixes:
-- - Function Search Path Mutable on public.set_updated_at
-- - Public/Signed-In Users Can Execute SECURITY DEFINER helper functions
--
-- What remains a UI setting:
-- - Leaked Password Protection Disabled must be enabled in Supabase Auth settings.

create schema if not exists private;
revoke all on schema private from public;
revoke all on schema private from anon;
revoke all on schema private from authenticated;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create or replace function private.is_account_member(p_account_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select exists (
        select 1
        from public.account_members m
        where m.account_id = p_account_id
          and m.user_id = auth.uid()
    );
$$;

create or replace function private.is_account_owner(p_account_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select exists (
        select 1
        from public.account_members m
        where m.account_id = p_account_id
          and m.user_id = auth.uid()
          and m.role = 'owner'
    );
$$;

create or replace function private.owns_account_record(p_account_id bigint)
returns boolean
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    select exists (
        select 1
        from public.accounts a
        where a.id = p_account_id
          and a.owner_user_id = auth.uid()
    );
$$;

revoke all on function private.is_account_member(bigint) from public;
revoke all on function private.is_account_member(bigint) from anon;
revoke all on function private.is_account_member(bigint) from authenticated;
revoke all on function private.is_account_owner(bigint) from public;
revoke all on function private.is_account_owner(bigint) from anon;
revoke all on function private.is_account_owner(bigint) from authenticated;
revoke all on function private.owns_account_record(bigint) from public;
revoke all on function private.owns_account_record(bigint) from anon;
revoke all on function private.owns_account_record(bigint) from authenticated;

drop policy if exists "accounts_select_member" on public.accounts;
create policy "accounts_select_member"
on public.accounts
for select
to authenticated
using (owner_user_id = auth.uid() or private.is_account_member(id));

drop policy if exists "accounts_update_owner" on public.accounts;
create policy "accounts_update_owner"
on public.accounts
for update
to authenticated
using (owner_user_id = auth.uid() or private.is_account_owner(id))
with check (owner_user_id = auth.uid() or private.is_account_owner(id));

drop policy if exists "account_members_select_member" on public.account_members;
create policy "account_members_select_member"
on public.account_members
for select
to authenticated
using (private.is_account_member(account_id) or private.owns_account_record(account_id));

drop policy if exists "account_members_insert_owner" on public.account_members;
create policy "account_members_insert_owner"
on public.account_members
for insert
to authenticated
with check (
    (
        user_id = auth.uid()
        and role = 'owner'
        and private.owns_account_record(account_id)
    )
    or private.is_account_owner(account_id)
);

drop policy if exists "account_members_update_owner" on public.account_members;
create policy "account_members_update_owner"
on public.account_members
for update
to authenticated
using (private.is_account_owner(account_id))
with check (private.is_account_owner(account_id));

drop policy if exists "account_members_delete_owner" on public.account_members;
create policy "account_members_delete_owner"
on public.account_members
for delete
to authenticated
using (private.is_account_owner(account_id));

drop policy if exists "user_consents_own_all" on public.user_consents;
create policy "user_consents_own_all"
on public.user_consents
for all
to authenticated
using (user_id = auth.uid() and private.is_account_member(account_id))
with check (user_id = auth.uid() and private.is_account_member(account_id));

drop policy if exists "properties_member_all" on public.properties;
create policy "properties_member_all"
on public.properties
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "pricing_rules_member_all" on public.pricing_rules;
create policy "pricing_rules_member_all"
on public.pricing_rules
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "guardrail_policies_member_all" on public.guardrail_policies;
create policy "guardrail_policies_member_all"
on public.guardrail_policies
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "price_calendar_member_all" on public.price_calendar;
create policy "price_calendar_member_all"
on public.price_calendar
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "decision_log_member_all" on public.decision_log;
create policy "decision_log_member_all"
on public.decision_log
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "occupancy_history_member_all" on public.occupancy_history;
create policy "occupancy_history_member_all"
on public.occupancy_history
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "market_history_member_all" on public.market_history;
create policy "market_history_member_all"
on public.market_history
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "telegram_links_member_all" on public.telegram_links;
create policy "telegram_links_member_all"
on public.telegram_links
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "telegram_approvals_member_all" on public.telegram_approvals;
create policy "telegram_approvals_member_all"
on public.telegram_approvals
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "property_integrations_member_all" on public.property_integrations;
create policy "property_integrations_member_all"
on public.property_integrations
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "operation_runs_member_all" on public.operation_runs;
create policy "operation_runs_member_all"
on public.operation_runs
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "audit_events_member_all" on public.audit_events;
create policy "audit_events_member_all"
on public.audit_events
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "notification_preferences_member_all" on public.notification_preferences;
create policy "notification_preferences_member_all"
on public.notification_preferences
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop policy if exists "notification_log_member_all" on public.notification_log;
create policy "notification_log_member_all"
on public.notification_log
for all
to authenticated
using (private.is_account_member(account_id))
with check (private.is_account_member(account_id));

drop function if exists public.is_account_member(bigint);
drop function if exists public.is_account_owner(bigint);
drop function if exists public.owns_account_record(bigint);
