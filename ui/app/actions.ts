"use server";

import { revalidatePath } from "next/cache";
import { supabase } from "@/lib/supabase";

const VALID_REACTIONS = ["love", "like", "meh", "no", "saved", "bought"] as const;
type ReactionType = (typeof VALID_REACTIONS)[number];

export async function react(formData: FormData) {
  const itemId = formData.get("itemId");
  const reaction = formData.get("reaction");

  if (typeof itemId !== "string" || typeof reaction !== "string") {
    throw new Error("itemId and reaction are required");
  }
  if (!VALID_REACTIONS.includes(reaction as ReactionType)) {
    throw new Error(`invalid reaction: ${reaction}`);
  }

  // Always insert a new row — we keep history so the learning loop can see
  // how taste evolved on a given item (e.g. saved → later bought).
  const { error } = await supabase.from("reactions").insert({
    item_id: itemId,
    reaction,
  });
  if (error) throw error;

  revalidatePath("/");
}
