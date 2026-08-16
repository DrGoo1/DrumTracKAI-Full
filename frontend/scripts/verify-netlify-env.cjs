/*
 * Fail the Netlify production build before publishing a broken reviewer portal.
 * This script prints presence/shape diagnostics only; it never prints key values.
 */

const context = String(process.env.CONTEXT || "unknown").trim();
const branch = String(process.env.BRANCH || "unknown").trim();

function value(name) {
  return String(process.env[name] || "").trim();
}

function safeOrigin(raw) {
  if (!raw) return null;
  try {
    return new URL(raw).origin;
  } catch (_error) {
    return "invalid-url";
  }
}

const apiBase = value("REACT_APP_API_BASE");
const supabaseUrl = value("REACT_APP_SUPABASE_URL");
const publishableKey =
  value("REACT_APP_SUPABASE_PUBLISHABLE_KEY") ||
  value("REACT_APP_SUPABASE_ANON_KEY");

const checks = {
  REACT_APP_API_BASE: Boolean(apiBase),
  REACT_APP_SUPABASE_URL: Boolean(supabaseUrl),
  REACT_APP_SUPABASE_PUBLISHABLE_KEY_OR_ANON_KEY: Boolean(publishableKey),
};

console.log("Calibration reviewer build-environment preflight");
console.log(`  CONTEXT=${context}`);
console.log(`  BRANCH=${branch}`);
console.log(
  `  REACT_APP_API_BASE present=${checks.REACT_APP_API_BASE} origin=${safeOrigin(apiBase)}`,
);
console.log(
  `  REACT_APP_SUPABASE_URL present=${checks.REACT_APP_SUPABASE_URL} origin=${safeOrigin(supabaseUrl)}`,
);
console.log(
  `  REACT_APP_SUPABASE_PUBLISHABLE_KEY_OR_ANON_KEY present=${checks.REACT_APP_SUPABASE_PUBLISHABLE_KEY_OR_ANON_KEY}`,
);

const missing = Object.entries(checks)
  .filter(([, present]) => !present)
  .map(([name]) => name);

const invalidUrls = [];
if (apiBase && safeOrigin(apiBase) === "invalid-url") {
  invalidUrls.push("REACT_APP_API_BASE");
}
if (supabaseUrl && safeOrigin(supabaseUrl) === "invalid-url") {
  invalidUrls.push("REACT_APP_SUPABASE_URL");
}

if (missing.length || invalidUrls.length) {
  if (missing.length) {
    console.error(`Missing required Netlify build variables: ${missing.join(", ")}`);
  }
  if (invalidUrls.length) {
    console.error(`Invalid URL values: ${invalidUrls.join(", ")}`);
  }
  console.error(
    "Set these on the exact Netlify site with Builds scope and Production context, then trigger a new production build.",
  );
  process.exit(2);
}

console.log("CALIBRATION_REVIEWER_BUILD_ENV_PASS");
