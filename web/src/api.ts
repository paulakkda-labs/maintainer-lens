export class ApiError extends Error {
  constructor(public status: number) {
    super(`HTTP ${status}`);
  }
}

export async function request<T>(
  path: string,
  signal?: AbortSignal,
  body?: unknown,
): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    signal,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json() as Promise<T>;
}

export function readSaved<T>(key: string, fallback: T, persistent = false): T {
  try {
    return (
      JSON.parse(
        (persistent ? localStorage : sessionStorage).getItem(key) || "null",
      ) ?? fallback
    );
  } catch {
    return fallback;
  }
}

export function save(key: string, value: unknown, persistent = false) {
  try {
    (persistent ? localStorage : sessionStorage).setItem(
      key,
      JSON.stringify(value),
    );
  } catch {
    /* Storage-disabled browsers still retain the in-memory workspace. */
  }
}
