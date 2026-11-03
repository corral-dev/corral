// Signed-in account, when the page is served behind an OAuth2 Proxy gateway. It only scopes
// saved drafts per account; locally (no gateway) the page runs as a single local user.

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
