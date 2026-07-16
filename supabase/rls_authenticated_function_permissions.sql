-- PricePilot: permessi necessari alle policy RLS per utenti autenticati.
--
-- Esegui questo script una sola volta per correggere progetti Supabase gia'
-- creati con una versione precedente dello schema. Non concede permessi ad
-- anon o public e non modifica alcun dato.

begin;

grant usage on schema private to authenticated;
grant execute on function private.is_account_member(bigint) to authenticated;
grant execute on function private.is_account_owner(bigint) to authenticated;
grant execute on function private.owns_account_record(bigint) to authenticated;

commit;

select 'rls_authenticated_function_permissions_ready' as status;
