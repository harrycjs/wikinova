/**
 * Minimal typed REST helpers for the new Jobs-style views.
 *
 * Reads the bootstrap token from a module-level variable that is set once
 * during the App boot phase. All requests carry the token as a Bearer header.
 */

export class ApiError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

// Module-level token, set once by App.tsx after bootstrap.
let _authToken = "";

/** Set the bootstrap token for all subsequent API calls. */
export function setApiToken(token: string): void {
  _authToken = token;
}

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (_authToken) h["Authorization"] = `Bearer ${_authToken}`;
  return h;
}

export const api = {
  async get<T>(path: string): Promise<T> {
    const res = await fetch(path, { headers: headers(), credentials: "same-origin" });
    if (!res.ok) {
      const body = await res.text();
      throw new ApiError(res.status, body, `GET ${path} failed: ${res.status}`);
    }
    return (await res.json()) as T;
  },
  async post<T>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(path, {
      method: "POST",
      headers: headers(),
      credentials: "same-origin",
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new ApiError(res.status, text, `POST ${path} failed: ${res.status}`);
    }
    return (await res.json()) as T;
  },
};

export { ApiError as ApiClientError };
