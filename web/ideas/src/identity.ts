// Signed-in account, when the page is served behind an OAuth2 Proxy gateway.
// Locally (no gateway) there is no account and the page runs as a single local user.

export interface Account {
  email: string;
}

const USERINFO = "/oauth2/userinfo";

export async function loadAccount(): Promise<Account | null> {
  try {
    const res = await fetch(USERINFO, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!res.ok || !res.headers.get("content-type")?.includes("json")) return null;
    const body = (await res.json()) as { email?: string; user?: string };
    const email = (body.email || body.user || "").trim().toLowerCase();
    return email ? { email } : null;
  } catch {
    return null;
  }
}

/**
 * Ends this site's session. A deployment may name where to go next (for example the
 * identity provider's account page, to switch accounts) with
 * `<meta name="ideas:after-sign-out" content="https://…">`; none is built in.
 */
export function signOutUrl(): string {
  const next = document.querySelector<HTMLMetaElement>('meta[name="ideas:after-sign-out"]')?.content?.trim();
  return next ? `/oauth2/sign_out?rd=${encodeURIComponent(next)}` : "/oauth2/sign_out";
}
