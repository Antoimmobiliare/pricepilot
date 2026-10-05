-- Durable Telegram getUpdates cursor for stateless GitHub Actions workers.
-- Server-only: no anon/authenticated access and no user-facing RLS policy.

begin;

create table if not exists public.telegram_update_cursor (
    consumer_key text primary key,
    next_update_id bigint not null default 0 check (next_update_id >= 0),
    last_update_id bigint,
    last_status text not null default 'ready',
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.telegram_update_cursor enable row level security;

revoke all on table public.telegram_update_cursor from anon, authenticated;
grant select, insert, update on table public.telegram_update_cursor to service_role;

drop trigger if exists set_telegram_update_cursor_updated_at
    on public.telegram_update_cursor;
create trigger set_telegram_update_cursor_updated_at
before update on public.telegram_update_cursor
for each row execute function public.set_updated_at();

commit;

select consumer_key, next_update_id, last_update_id, last_status, updated_at
from public.telegram_update_cursor
order by consumer_key;
