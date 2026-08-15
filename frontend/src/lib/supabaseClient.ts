import { createClient, SupabaseClient } from "@supabase/supabase-js";

const supabaseUrl = String(process.env.REACT_APP_SUPABASE_URL || "").trim();
const supabasePublishableKey = String(
  process.env.REACT_APP_SUPABASE_PUBLISHABLE_KEY
    || process.env.REACT_APP_SUPABASE_ANON_KEY
    || "",
).trim();

export const supabaseConfigurationError = !supabaseUrl
  ? "REACT_APP_SUPABASE_URL is not configured."
  : !supabasePublishableKey
    ? "REACT_APP_SUPABASE_PUBLISHABLE_KEY is not configured."
    : null;

export const supabase: SupabaseClient | null = supabaseConfigurationError
  ? null
  : createClient(supabaseUrl, supabasePublishableKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    });
