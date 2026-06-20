import { getFeed } from "@/lib/queries";
import { react } from "./actions";

const REACTIONS: { value: string; label: string; emoji: string }[] = [
  { value: "love", label: "Love", emoji: "❤️" },
  { value: "like", label: "Like", emoji: "👍" },
  { value: "meh", label: "Meh", emoji: "😐" },
  { value: "no", label: "No", emoji: "🚫" },
  { value: "saved", label: "Saved", emoji: "🔖" },
  { value: "bought", label: "Bought", emoji: "🛍️" },
];

export default async function Home() {
  const feed = await getFeed();

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Surfaced for you</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {feed.length} item{feed.length === 1 ? "" : "s"} above the surfacing threshold. React to teach the agent.
        </p>
      </header>

      {feed.length === 0 ? (
        <p className="rounded-lg border border-dashed border-zinc-300 p-8 text-center text-zinc-500">
          No surfaced items yet. Score some via{" "}
          <code className="rounded bg-zinc-100 px-1 py-0.5 text-xs">
            uv run scripts/score_cli.py
          </code>{" "}
          and they&apos;ll appear here.
        </p>
      ) : (
        <ul className="space-y-8">
          {feed.map(({ item, scoringRun, latestReaction }) => (
            <li
              key={item.id}
              className="overflow-hidden rounded-xl border border-zinc-200 bg-white"
            >
              <div className="grid grid-cols-1 sm:grid-cols-[260px_1fr]">
                {item.image_urls && item.image_urls.length > 0 ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={item.image_urls[0]}
                    alt={item.title ?? item.brand ?? "Item"}
                    className="h-72 w-full object-cover sm:h-full"
                  />
                ) : (
                  <div className="flex h-72 items-center justify-center bg-zinc-100 text-xs text-zinc-400 sm:h-full">
                    no image
                  </div>
                )}

                <div className="p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs uppercase tracking-wider text-zinc-500">
                        {item.brand ?? "Unknown brand"}
                      </p>
                      <h2 className="mt-0.5 text-base font-medium leading-snug">
                        {item.title ?? "Untitled"}
                      </h2>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="text-lg font-semibold">
                        {item.price != null ? `$${item.price.toFixed(2)}` : "—"}
                      </p>
                      <p className="text-xs text-zinc-500">
                        size {item.size ?? "—"} · {item.condition ?? "—"}
                      </p>
                    </div>
                  </div>

                  <div className="mt-3 flex items-center gap-3 text-xs">
                    <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 font-medium text-emerald-800">
                      score {scoringRun.taste_score}
                    </span>
                    <span className="text-zinc-500">
                      adjusted {Number(scoringRun.price_adjusted_score).toFixed(2)}
                    </span>
                    {scoringRun.features_hit && scoringRun.features_hit.length > 0 && (
                      <span className="text-zinc-500">
                        features {scoringRun.features_hit.join(", ")}
                      </span>
                    )}
                  </div>

                  {scoringRun.reasoning && (
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs text-zinc-500 hover:text-zinc-900">
                        why?
                      </summary>
                      <p className="mt-2 text-sm leading-relaxed text-zinc-700">
                        {scoringRun.reasoning}
                      </p>
                    </details>
                  )}

                  {scoringRun.flags && scoringRun.flags.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {scoringRun.flags.map((flag) => (
                        <span
                          key={flag}
                          className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800"
                        >
                          ⚠ {flag}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="mt-5 flex flex-wrap items-center gap-2">
                    {REACTIONS.map(({ value, label, emoji }) => {
                      const isLatest = latestReaction?.reaction === value;
                      return (
                        <form key={value} action={react}>
                          <input type="hidden" name="itemId" value={item.id} />
                          <input type="hidden" name="reaction" value={value} />
                          <button
                            type="submit"
                            className={
                              "rounded-full border px-3 py-1.5 text-sm transition " +
                              (isLatest
                                ? "border-zinc-900 bg-zinc-900 text-white"
                                : "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-400")
                            }
                          >
                            <span className="mr-1">{emoji}</span>
                            {label}
                          </button>
                        </form>
                      );
                    })}
                  </div>

                  {item.url && (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-4 inline-block text-xs text-zinc-500 underline-offset-2 hover:text-zinc-900 hover:underline"
                    >
                      view on source ↗
                    </a>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
