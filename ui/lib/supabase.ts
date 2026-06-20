import { createClient } from "@supabase/supabase-js";

// Server-only Supabase client. Uses the service role key, which bypasses
// Row Level Security — fine because every call originates from server
// components or server actions (the key never reaches the browser).
//
// If this app ever serves multiple users, switch to the anon key + RLS
// policies. For now (one user, one taste), service role is simplest.
const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.SUPABASE_SERVICE_KEY;

if (!url || !key) {
  throw new Error(
    "Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_KEY in ui/.env.local"
  );
}

export const supabase = createClient(url, key, {
  auth: { persistSession: false },
});

export type Item = {
  id: string;
  source: string;
  url: string | null;
  brand: string | null;
  category: string | null;
  subcategory: string | null;
  title: string | null;
  description: string | null;
  price: number | null;
  size: string | null;
  condition: string | null;
  color: string | null;
  material: string | null;
  image_urls: string[] | null;
};

export type ScoringRun = {
  id: string;
  item_id: string;
  taste_score: number;
  price_adjusted_score: number;
  reasoning: string | null;
  features_hit: string[] | null;
  sub_aesthetics: string[] | null;
  flags: string[] | null;
  surfaced: boolean;
  scored_at: string;
};

export type Reaction = {
  id: string;
  item_id: string;
  reaction: "love" | "like" | "meh" | "no" | "saved" | "bought";
  note: string | null;
  reacted_at: string;
};

export type FeedItem = {
  item: Item;
  scoringRun: ScoringRun;
  latestReaction: Reaction | null;
};
