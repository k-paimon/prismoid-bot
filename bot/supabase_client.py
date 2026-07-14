"""
Minimal Supabase (PostgREST) client — pure stdlib, like the exchange clients.

Talks to {SUPABASE_URL}/rest/v1 with the service-role key, which bypasses RLS;
this module is for trusted server-side code (the VM cloud agent) only.

    sb = Supabase()                      # reads SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
    sb.select("bot_commands", "status=eq.pending&order=id.asc&limit=5")
    sb.insert("bot_logs", rows, upsert=True)
    sb.update("bot_commands", "id=eq.7&status=eq.pending", {"status": "running"})
    sb.upsert("bot_state", {"id": 1, "running": True})
    sb.delete("bot_logs", "seq=lt.100")

Filter strings are raw PostgREST query syntax (eq., gt., in.(...), order=...).
"""
import json
import os
import urllib.error
import urllib.request


class SupabaseError(Exception):
    def __init__(self, status, body):
        super().__init__(f"supabase {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


class Supabase:
    def __init__(self, url=None, key=None, timeout=15):
        # strip whitespace and BOMs that env tooling can smuggle in
        junk = "\ufeff \t\r\n"
        self.url = (url or os.environ.get("SUPABASE_URL", "")) \
            .strip(junk).rstrip("/")
        self.key = (key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")) \
            .strip(junk)
        self.timeout = timeout
        if not (self.url and self.key):
            raise SupabaseError(0, "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY "
                                   "must be set")

    def _request(self, method, table, query="", body=None, prefer=None):
        url = f"{self.url}/rest/v1/{table}" + (f"?{query}" if query else "")
        headers = {"apikey": self.key,
                   "Authorization": f"Bearer {self.key}",
                   "Content-Type": "application/json",
                   # sb_secret_ keys are rejected for browser-looking clients
                   "User-Agent": "gridbot-cloud-agent/0.1"}
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise SupabaseError(e.code, e.read().decode(errors="replace"))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SupabaseError(0, str(e))
        return json.loads(raw) if raw else None

    def select(self, table, query=""):
        return self._request("GET", table, query)

    def insert(self, table, rows, upsert=False):
        prefer = "return=minimal"
        if upsert:
            prefer += ",resolution=merge-duplicates"
        return self._request("POST", table, body=rows, prefer=prefer)

    def upsert(self, table, row):
        return self.insert(table, row, upsert=True)

    def update(self, table, query, patch):
        """PATCH matching rows; returns the updated rows (so a conditional
        claim like id=eq.X&status=eq.pending can detect a lost race)."""
        return self._request("PATCH", table, query, body=patch,
                             prefer="return=representation")

    def delete(self, table, query):
        if not query:
            raise SupabaseError(0, "refusing to delete without a filter")
        return self._request("DELETE", table, query, prefer="return=minimal")
