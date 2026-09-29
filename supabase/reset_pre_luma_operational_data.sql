-- Reset PricePilot application data before configuring Luma Pisa.
-- Keeps auth.users and public.profiles intact so the existing Supabase login can
-- create one clean account on the next successful access. No credentials, OTA
-- mappings or external systems are contacted by this SQL.
begin;

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'pricing_date_locks', 'operational_documents', 'price_updates',
    'pricing_decisions', 'competitors', 'events', 'market_snapshots',
    'notification_log', 'notification_preferences', 'audit_events',
    'operation_runs', 'property_integrations', 'telegram_approvals',
    'telegram_links', 'market_history', 'occupancy_history', 'decision_log',
    'price_calendar', 'guardrail_policies', 'pricing_rules', 'properties',
    'user_consents', 'account_members', 'app_sessions'
  ]
  loop
    if to_regclass('public.' || table_name) is not null then
      execute format('delete from public.%I', table_name);
    end if;
  end loop;
end $$;

delete from public.accounts;

commit;

-- Verification: all values must be zero. auth.users and public.profiles remain.
select
  (select count(*) from public.accounts) as accounts,
  (select count(*) from public.properties) as properties,
  (select count(*) from public.decision_log) as decisions,
  (select count(*) from public.operational_documents) as operational_documents;
