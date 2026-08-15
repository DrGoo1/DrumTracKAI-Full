-- DrumTracKAI Calibration v2
-- Apply through Supabase SQL Editor or the normal migration runner.
-- This migration is additive and does not delete legacy calibration data.

begin;

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Authentication / reviewer linkage
-- ---------------------------------------------------------------------------
create table if not exists public.app_user_roles (
  user_id uuid not null,
  role text not null,
  created_at timestamptz not null default now(),
  primary key (user_id, role)
);

create table if not exists public.user_drummer_map (
  user_id uuid not null,
  drummer_id text not null,
  created_at timestamptz not null default now(),
  primary key (user_id, drummer_id)
);

alter table public.reviewer_profiles
  add column if not exists auth_user_id uuid,
  add column if not exists consent_version text,
  add column if not exists consented_at timestamptz,
  add column if not exists is_active boolean not null default true,
  add column if not exists updated_at timestamptz not null default now();

create unique index if not exists uq_reviewer_profiles_auth_user
  on public.reviewer_profiles(auth_user_id)
  where auth_user_id is not null;

create index if not exists idx_reviewer_profiles_active
  on public.reviewer_profiles(is_active, reviewer_id);

-- ---------------------------------------------------------------------------
-- Controlled treatment definitions
-- ---------------------------------------------------------------------------
create table if not exists public.calibration_treatments (
  treatment_id text primary key,
  drummer_slug text not null,
  name text not null,
  description text not null default '',
  status text not null default 'draft',
  base_model_version text,
  cfg_overrides_json jsonb not null default '{}'::jsonb,
  profile_overrides_json jsonb not null default '{}'::jsonb,
  created_by uuid not null,
  created_at timestamptz not null default now(),
  activated_at timestamptz,
  retired_at timestamptz,
  constraint calibration_treatments_status_check
    check (status in ('draft', 'active', 'retired'))
);

create index if not exists idx_calibration_treatments_drummer_status
  on public.calibration_treatments(drummer_slug, status, created_at desc);

-- ---------------------------------------------------------------------------
-- Immutable experimental trial record.  Hidden mapping stays server-side.
-- ---------------------------------------------------------------------------
create table if not exists public.calibration_trials (
  trial_id text primary key,
  item_id text not null unique,
  session_id text not null,
  reviewer_id text not null,
  drummer_slug text not null,
  base_groove_id text not null,
  neutral_run_id text not null,
  control_run_id text not null,
  challenger_run_id text not null,
  visible_a_run_id text not null,
  visible_b_run_id text not null,
  challenger_treatment_id text not null,
  paired_seed bigint not null,
  assignment_seed bigint not null,
  control_profile_hash text not null,
  challenger_profile_hash text not null,
  control_profile_snapshot_json jsonb not null,
  challenger_profile_snapshot_json jsonb not null,
  assignment_json jsonb not null,
  generation_metadata_json jsonb not null default '{}'::jsonb,
  model_version text,
  renderer_version text,
  sample_pack_version text,
  status text not null default 'queued',
  error_text text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint calibration_trials_status_check
    check (status in ('queued', 'ready', 'completed', 'failed', 'cancelled')),
  constraint calibration_trials_distinct_candidates_check
    check (control_run_id <> challenger_run_id),
  constraint calibration_trials_visible_pair_check
    check (
      (visible_a_run_id = control_run_id and visible_b_run_id = challenger_run_id)
      or
      (visible_a_run_id = challenger_run_id and visible_b_run_id = control_run_id)
    )
);

create index if not exists idx_calibration_trials_reviewer_status
  on public.calibration_trials(reviewer_id, status, created_at);
create index if not exists idx_calibration_trials_drummer
  on public.calibration_trials(drummer_slug, created_at desc);
create index if not exists idx_calibration_trials_treatment
  on public.calibration_trials(challenger_treatment_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Reviewer response provenance / idempotency
-- ---------------------------------------------------------------------------
alter table public.pairwise_judgments
  add column if not exists reviewer_id text,
  add column if not exists technical_issue boolean not null default false,
  add column if not exists cannot_judge boolean not null default false,
  add column if not exists comment text,
  add column if not exists listening_ms bigint not null default 0,
  add column if not exists candidate_a_listening_ms bigint not null default 0,
  add column if not exists candidate_b_listening_ms bigint not null default 0,
  add column if not exists candidate_a_play_count integer not null default 0,
  add column if not exists candidate_b_play_count integer not null default 0,
  add column if not exists idempotency_key text;

alter table public.attribute_ratings
  add column if not exists reviewer_id text,
  add column if not exists idempotency_key text;

update public.pairwise_judgments j
set reviewer_id = s.reviewer_id
from public.evaluation_items i
join public.evaluation_sessions s on s.session_id = i.session_id
where i.item_id = j.item_id
  and j.reviewer_id is null;

update public.attribute_ratings r
set reviewer_id = s.reviewer_id
from public.evaluation_items i
join public.evaluation_sessions s on s.session_id = i.session_id
where i.item_id = r.item_id
  and r.reviewer_id is null;

create unique index if not exists uq_pairwise_idempotency
  on public.pairwise_judgments(idempotency_key)
  where idempotency_key is not null;
create unique index if not exists uq_attribute_idempotency
  on public.attribute_ratings(idempotency_key)
  where idempotency_key is not null;
create unique index if not exists uq_pairwise_reviewer_item_v2
  on public.pairwise_judgments(reviewer_id, item_id)
  where reviewer_id is not null and idempotency_key is not null;
create unique index if not exists uq_attribute_reviewer_item_label_v2
  on public.attribute_ratings(reviewer_id, item_id, candidate_label)
  where reviewer_id is not null and idempotency_key is not null;
create index if not exists idx_pairwise_reviewer_item
  on public.pairwise_judgments(reviewer_id, item_id, created_at desc);
create index if not exists idx_attribute_reviewer_item
  on public.attribute_ratings(reviewer_id, item_id, candidate_label);

-- Add constraints as NOT VALID so legacy rows do not block the migration.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'pairwise_preferred_choice_check'
  ) then
    alter table public.pairwise_judgments
      add constraint pairwise_preferred_choice_check
      check (preferred_candidate is null or preferred_candidate in ('A','B','tie','neither')) not valid;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'pairwise_closer_choice_check'
  ) then
    alter table public.pairwise_judgments
      add constraint pairwise_closer_choice_check
      check (closer_to_target is null or closer_to_target in ('A','B','tie','neither')) not valid;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'pairwise_feel_choice_check'
  ) then
    alter table public.pairwise_judgments
      add constraint pairwise_feel_choice_check
      check (better_feel is null or better_feel in ('A','B','tie','neither')) not valid;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'pairwise_musical_choice_check'
  ) then
    alter table public.pairwise_judgments
      add constraint pairwise_musical_choice_check
      check (more_musical is null or more_musical in ('A','B','tie','neither')) not valid;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'attribute_candidate_choice_check'
  ) then
    alter table public.attribute_ratings
      add constraint attribute_candidate_choice_check
      check (candidate_label in ('A','B','single')) not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'pairwise_confidence_range_v2') then
    alter table public.pairwise_judgments
      add constraint pairwise_confidence_range_v2
      check (confidence is null or confidence between 1 and 5) not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'pairwise_listening_nonnegative_v2') then
    alter table public.pairwise_judgments
      add constraint pairwise_listening_nonnegative_v2
      check (listening_ms >= 0 and candidate_a_listening_ms >= 0 and candidate_b_listening_ms >= 0) not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'attribute_ratings_range_v2') then
    alter table public.attribute_ratings
      add constraint attribute_ratings_range_v2
      check (
        (stylistic_authenticity is null or stylistic_authenticity between 1 and 10) and
        (groove_feel is null or groove_feel between 1 and 10) and
        (dynamics is null or dynamics between 1 and 10) and
        (phrasing is null or phrasing between 1 and 10) and
        (kit_balance is null or kit_balance between 1 and 10) and
        (fill_behavior is null or fill_behavior between 1 and 10) and
        (human_realism is null or human_realism between 1 and 10) and
        (overall_usefulness is null or overall_usefulness between 1 and 10)
      ) not valid;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Referential integrity for the existing calibration tables
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'evaluation_items_session_fk_v2') then
    alter table public.evaluation_items
      add constraint evaluation_items_session_fk_v2
      foreign key (session_id) references public.evaluation_sessions(session_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'evaluation_items_baseline_run_fk_v2') then
    alter table public.evaluation_items
      add constraint evaluation_items_baseline_run_fk_v2
      foreign key (baseline_run_id) references public.calibration_runs(run_id)
      on delete set null not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'evaluation_items_candidate_a_fk_v2') then
    alter table public.evaluation_items
      add constraint evaluation_items_candidate_a_fk_v2
      foreign key (candidate_a_run_id) references public.calibration_runs(run_id)
      on delete set null not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'evaluation_items_candidate_b_fk_v2') then
    alter table public.evaluation_items
      add constraint evaluation_items_candidate_b_fk_v2
      foreign key (candidate_b_run_id) references public.calibration_runs(run_id)
      on delete set null not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'pairwise_item_fk_v2') then
    alter table public.pairwise_judgments
      add constraint pairwise_item_fk_v2
      foreign key (item_id) references public.evaluation_items(item_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'attribute_item_fk_v2') then
    alter table public.attribute_ratings
      add constraint attribute_item_fk_v2
      foreign key (item_id) references public.evaluation_items(item_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'run_versions_run_fk_v2') then
    alter table public.run_versions
      add constraint run_versions_run_fk_v2
      foreign key (run_id) references public.calibration_runs(run_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'calibration_trials_item_fk') then
    alter table public.calibration_trials
      add constraint calibration_trials_item_fk
      foreign key (item_id) references public.evaluation_items(item_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'calibration_trials_session_fk') then
    alter table public.calibration_trials
      add constraint calibration_trials_session_fk
      foreign key (session_id) references public.evaluation_sessions(session_id)
      on delete cascade not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'calibration_trials_treatment_fk') then
    alter table public.calibration_trials
      add constraint calibration_trials_treatment_fk
      foreign key (challenger_treatment_id) references public.calibration_treatments(treatment_id)
      on delete restrict not valid;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'calibration_trials_reviewer_fk') then
    alter table public.calibration_trials
      add constraint calibration_trials_reviewer_fk
      foreign key (reviewer_id) references public.reviewer_profiles(reviewer_id)
      on delete restrict not valid;
  end if;
end $$;

-- The browser must not write these tables directly. All writes go through FastAPI.
revoke all on public.calibration_treatments from anon, authenticated;
revoke all on public.calibration_trials from anon, authenticated;
revoke all on public.pairwise_judgments from anon, authenticated;
revoke all on public.attribute_ratings from anon, authenticated;

-- Keep role/mapping tables readable only through the API for this implementation.
revoke all on public.app_user_roles from anon, authenticated;
revoke all on public.user_drummer_map from anon, authenticated;

commit;

-- After orphan cleanup, validate the NOT VALID constraints in a separate maintenance step.
-- Example:
-- alter table public.evaluation_items validate constraint evaluation_items_session_fk_v2;
-- alter table public.pairwise_judgments validate constraint pairwise_item_fk_v2;
-- alter table public.attribute_ratings validate constraint attribute_item_fk_v2;
