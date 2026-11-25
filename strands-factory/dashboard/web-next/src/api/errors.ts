/**
 * Typed failures of the dashboard API (PortalApi). Views branch on these instead of regex-matching
 * error strings; both the live client and the mock throw them.
 */

/**
 * A non-2xx dashboard-API response: `status`, plus the server's `code` and `error` text when its
 * JSON body named them.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly serverError?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * 409 `cursor_expired` from a paginated transcript read: the history before the cursor changed
 * (expiry, a rolled ledger window, reordering) so the older page cannot be stitched on without
 * skipping or repeating messages. The only recovery is a fresh first page.
 */
export class CursorExpiredError extends ApiError {
  constructor(message = "History changed — reload") {
    super(message, 409, "cursor_expired");
    this.name = "CursorExpiredError";
  }
}

/** The server's `code` out of an error body like `{"error": "...", "code": "cursor_expired"}`. */
export function errorCode(body: string): string | undefined {
  try {
    const parsed = JSON.parse(body) as { code?: unknown };
    return typeof parsed.code === "string" ? parsed.code : undefined;
  } catch {
    return undefined;
  }
}

/** The server's `error` text out of an error body like `{"error": "..."}`. */
export function errorText(body: string): string | undefined {
  try {
    const parsed = JSON.parse(body) as { error?: unknown };
    return typeof parsed.error === "string" && parsed.error ? parsed.error : undefined;
  } catch {
    return undefined;
  }
}
