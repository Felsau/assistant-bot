-- Supabase / Postgres schema for the Personal Assistant Bot.
-- Run this in the Supabase SQL editor (or psql) before starting the bot.

create table if not exists notes (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  content text not null,
  created_at timestamptz default now()
);

create table if not exists schedule (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  title text not null,
  day_of_week text,        -- e.g. 'Monday'
  start_time time,
  end_time time,
  location text,
  notes text,
  created_at timestamptz default now()
);

create table if not exists tasks (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  title text not null,
  due_date date,
  is_done boolean default false,
  priority text default 'normal',  -- low / normal / high
  created_at timestamptz default now()
);

-- Known users, so scheduled jobs (e.g. the morning digest) know where to send.
create table if not exists users (
  user_id text primary key,
  chat_id bigint not null,
  created_at timestamptz default now()
);

-- Helpful indexes for per-user lookups.
create index if not exists notes_user_idx on notes (user_id, created_at desc);
create index if not exists schedule_user_idx on schedule (user_id, day_of_week);
create index if not exists tasks_user_idx on tasks (user_id, due_date);
