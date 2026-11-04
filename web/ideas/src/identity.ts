// Signed-in account, when the page is served behind an OAuth2 Proxy gateway. It only scopes
// saved drafts per account; locally (no gateway) the page runs as a single local user.

export interface Account {
  email: string;
}

const USERINFO = "/oauth2/userinfo";

/** Only an explicit loopback static preview has an anonymous single-user workspace. */
export async function loadAccount(): Promise<Account | null> {
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const response = await fetch(USERINFO, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (loopback && response.status === 404) return null;
  if (!response.ok || !response.headers.get("content-type")?.includes("json")) throw new Error("Account verification unavailable.");
  const body: unknown = await response.json();
  if (typeof body !== "object" || body === null) throw new Error("Account response is invalid.");
  const email = "email" in body && typeof body.email === "string" ? body.email : "user" in body && typeof body.user === "string" ? body.user : "";
  const normalized = email.trim().toLowerCase();
  if (!normalized) throw new Error("Account identity is missing.");
  return { email: normalized };
}
