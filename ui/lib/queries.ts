import { supabase, type FeedItem, type Item, type Reaction, type ScoringRun } from "./supabase";

/**
 * Returns surfaced items for the feed.
 *
 * For each item we pick the *latest* scoring_run (so re-scores under a new
 * profile version replace older ones in the UI), and attach the latest
 * reaction so the card can show the user's last verdict.
 *
 * Personal-scale dataset — dedupe-in-JS is plenty. If this grows past a few
 * thousand items we can move this to a SQL view or RPC.
 */
export async function getFeed(): Promise<FeedItem[]> {
  const { data: runs, error: runsError } = await supabase
    .from("scoring_runs")
    .select("*, items(*)")
    .eq("surfaced", true)
    .order("scored_at", { ascending: false });
  if (runsError) throw runsError;

  const latestRunByItem = new Map<string, ScoringRun & { items: Item }>();
  for (const run of runs ?? []) {
    if (!latestRunByItem.has(run.item_id)) {
      latestRunByItem.set(run.item_id, run);
    }
  }

  const { data: reactions, error: reactionsError } = await supabase
    .from("reactions")
    .select("*")
    .order("reacted_at", { ascending: false });
  if (reactionsError) throw reactionsError;

  const latestReactionByItem = new Map<string, Reaction>();
  for (const r of reactions ?? []) {
    if (!latestReactionByItem.has(r.item_id)) {
      latestReactionByItem.set(r.item_id, r);
    }
  }

  return Array.from(latestRunByItem.values()).map(({ items, ...scoringRun }) => ({
    item: items,
    scoringRun,
    latestReaction: latestReactionByItem.get(scoringRun.item_id) ?? null,
  }));
}
