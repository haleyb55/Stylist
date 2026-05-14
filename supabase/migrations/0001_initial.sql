-- ============================================================================
-- 0001_initial.sql — Initial schema for the taste agent
-- ============================================================================
-- Four tables, designed multi-source from day one so adding Vestiaire, Grailed,
-- etc. later requires no schema changes:
--
--   items            every product seen, across any resale source
--   profile_versions versioned taste profile (markdown); only one is_active
--   scoring_runs     one row per (item, profile_version) scoring attempt
--   reactions        my love/like/meh/no/saved/bought reactions on items
--
-- Field shapes for scoring_runs (features_hit, sub_aesthetics, flags, etc.)
-- mirror the JSON the scoring prompt is specified to return in profile.md
-- under "Scoring guidance for Claude".
-- ============================================================================


-- ---------------------------------------------------------------------------
-- items: the universal product table.
--
-- The PK is a composite text id like 'trr_12345' so the namespace is global
-- across sources — we can hold a TRR listing and a Vestiaire listing of the
-- "same" item without collision, and IDs are stable + human-readable in logs.
-- (source, source_item_id) is also unique so upserts from the scraper are
-- straightforward.
--
-- raw_payload (jsonb) holds the full parsed listing for forward compatibility:
-- if we discover we want a new field later, we don't have to re-scrape.
-- ---------------------------------------------------------------------------
create table items (
    id              text primary key,                       -- e.g. 'trr_12345'
    source          text not null default 'trr',            -- 'trr' | 'vestiaire' | 'grailed' | ...
    source_item_id  text not null,                          -- the site's own listing id
    url             text,
    brand           text,
    category        text,                                   -- TRR's top-level (clothing, shoes, bags, ...)
    subcategory     text,                                   -- TRR's subcategory (dresses, boots, ...)
    title           text,
    description     text,
    price           numeric(10, 2),
    size            text,                                   -- free text — sites use wildly different conventions
    condition       text,                                   -- TRR: 'Pristine' | 'Excellent' | 'Very Good' | 'Good' | 'Fair'
    color           text,
    material        text,
    image_urls      text[],
    listed_at       timestamptz,                            -- when the listing went live on the source site
    first_seen_at   timestamptz not null default now(),     -- when we first scraped it
    raw_payload     jsonb,
    unique (source, source_item_id)
);

create index items_brand_idx        on items (brand);
create index items_category_idx     on items (category);
create index items_source_seen_idx  on items (source, first_seen_at desc);


-- ---------------------------------------------------------------------------
-- profile_versions: versioned taste profile.
--
-- profile_text is the full markdown of profile.md. We never overwrite — the
-- learning loop creates a new row and flips is_active. The audit trail matters
-- because Sonnet's profile rewrites need to be reviewable + reversible.
--
-- generated_from_reaction_count records how many reactions had been logged
-- when this version was generated, so we can correlate profile drift with
-- the data that drove it.
-- ---------------------------------------------------------------------------
create table profile_versions (
    id                              uuid primary key default gen_random_uuid(),
    profile_text                    text not null,
    generated_from_reaction_count   int,                    -- null for the bootstrap version
    notes                           text,                   -- changelog: what changed and why
    is_active                       bool not null default false,
    created_at                      timestamptz not null default now()
);

-- Enforce "only one active profile" at the database level so a buggy learning
-- loop can't leave us with two active versions silently.
create unique index profile_versions_one_active_idx
    on profile_versions (is_active)
    where is_active = true;


-- ---------------------------------------------------------------------------
-- scoring_runs: one row per scoring attempt.
--
-- An item gets re-scored when the active profile version changes, so (item_id,
-- profile_version_id) is a natural pairing. We keep all historical scores
-- (don't dedupe) so we can study how taste evolves vs. how scoring changes.
--
-- taste_score is the raw Haiku output (0-10, profile-only).
-- price_adjusted_score is what we get after applying the price multiplier in
-- code — stored so the UI can sort/filter without recomputing.
-- surfaced records whether this item passed the category threshold and showed
-- up in the feed. Partial index makes "what's in my feed" queries fast.
-- ---------------------------------------------------------------------------
create table scoring_runs (
    id                      uuid primary key default gen_random_uuid(),
    item_id                 text not null references items(id) on delete cascade,
    profile_version_id      uuid not null references profile_versions(id),
    taste_score             numeric(4, 2) check (taste_score >= 0 and taste_score <= 10),
    price_adjusted_score    numeric(4, 2),
    reasoning               text,                           -- 2-3 sentences referencing profile elements
    features_hit            text[],                         -- e.g. ['1', '2', '4b'] — priority features from profile.md
    sub_aesthetics          text[],                         -- e.g. ['romantic prairie / 70s', 'workwear / utility']
    flags                   text[],                         -- concerns: 'wrong size', 'plain piece', 'too flattering-coded'
    surfaced                bool not null default false,
    scored_at               timestamptz not null default now()
);

create index scoring_runs_item_idx     on scoring_runs (item_id);
create index scoring_runs_surfaced_idx on scoring_runs (surfaced) where surfaced = true;


-- ---------------------------------------------------------------------------
-- reactions: my labelled feedback. This is the training signal for the
-- learning loop.
--
-- One item can have multiple reactions over time (e.g. 'saved' then later
-- 'bought'); the learning loop should look at the most recent reaction per
-- item, but keep history so we can spot taste-changes-over-time patterns.
--
-- Reaction set is constrained to the six values the UI offers. If we add a
-- new reaction type later, the migration is a one-liner.
-- ---------------------------------------------------------------------------
create table reactions (
    id          uuid primary key default gen_random_uuid(),
    item_id     text not null references items(id) on delete cascade,
    reaction    text not null check (reaction in ('love', 'like', 'meh', 'no', 'saved', 'bought')),
    note        text,                                       -- optional free text
    reacted_at  timestamptz not null default now()
);

create index reactions_item_idx       on reactions (item_id);
create index reactions_reacted_at_idx on reactions (reacted_at desc);
