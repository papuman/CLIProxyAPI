#!/usr/bin/env python3
"""reset-keeper: spends Claude reset grants for the CLIProxyAPI pool, automatically.

Rules ported from oh-my-pi (packages/coding-agent/src/session/claude-auto-reset.ts),
changed to suit a pool of single-reset Team accounts:

WEEKLY ONLY. A reset clears both windows, but a 5-hour block refills by itself within
hours, so only a used-up WEEKLY window (>= 99.9%) ever triggers a reset. A 5-hour block,
whatever the weekly usage, never does: the account just waits for its 5-hour refill.

RESTORE. Each grant can only reset its own account, so accounts never compete for one and
the pool running dry is not a reason to spend. A reset sets weekly usage back to 0 but keeps
the window's end date (checked 2026-10-08), so its worth is the use it buys before then.
In hours of nonstop use, with B = hours to burn one full week nonstop (learned from the
proxy's numbers: how much of the week one full 5-hour window costs), W = hours until the
weekly window refills by itself, and E = hours until the grant expires:
  - spend now buys min(W, B);
  - waiting buys B if, after the natural refill, a full week can be burned again before E
    (a later block is then reset at full worth), else the use made by E, which the
    last-hour SALVAGE gives back.
It spends now only if that buys at least as much as waiting. So: a block that refills in 6 h
waits (even if all coding stops), unless the grant expires too soon for a later reset to
be worth more.
The grant must also be usable now, unexpired, clear the weekly window, Anthropic must show
no reset cooldown, and KEEP_RESETS must remain after. Checks run every CHECK_EVERY_SECONDS
(default 10) from the proxy's own numbers, which costs nothing; Anthropic is only asked
about an account the proxy already sees at 100% weekly.

SALVAGE (grant about to expire). Every SALVAGE_EVERY_MINUTES (default 30): a grant ending
within SALVAGE_HORIZON_HOURS (default 1 h, as late as is safe, so the account is used as
much as possible first) is spent, whatever the usage. An unspent grant is lost at expiry.

After every spend that Anthropic confirms, it clears the proxy's own cooldown for that
account (POST /v0/management/reset-quota), which the proxy never does by itself.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass, field
from datetime import datetime, timezone

CLAUDE_HEADERS = {
    "User-Agent": "claude-cli/2.1.280 (external, cli)",
    "Authorization": "Bearer $TOKEN$",  # the proxy swaps in the account's own token
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",
}
ANTHROPIC = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage?cedar_ember=1&skip_spend=1"
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
# Anthropic answers 429 when one account's usage is read more often than about every 3 min.
READ_GAP_S = 200
# A used-up weekly window stays used up until its fixed end date, so re-reading it often
# only spends the per-account read budget the dashboard also needs.
FULL_REREAD_S = 15 * 60
# The keeper is the only reader of Anthropic's usage page; the dashboard takes its copy.
# One account per tick, each about every 10 min, so 8 accounts never burst.
REFRESH_S = 10 * 60
RATE_LIMIT_BACKOFF_S = 5 * 60
PROFILE_REFRESH_S = 24 * 3600
IMMINENT_EXPIRY_S = 5 * 60


def env_num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Settings:
    enabled: bool = os.environ.get("ENABLED", "true").lower() == "true"
    dry_run: bool = os.environ.get("DRY_RUN", "false").lower() == "true"
    check_every_s: float = env_num("CHECK_EVERY_SECONDS", 10)
    # Used until enough traffic has been seen to learn the real burn time.
    burn_hours: float = env_num("BURN_HOURS", 50)
    keep_resets: int = int(env_num("KEEP_RESETS", 0))
    salvage_every_minutes: float = env_num("SALVAGE_EVERY_MINUTES", 30)
    salvage_horizon_hours: float = env_num("SALVAGE_HORIZON_HOURS", 1)
    salvage_min_used_percent: float = env_num("SALVAGE_MIN_USED_PERCENT", 0)


# ---------------------------------------------------------------- pure decisions


def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def account_can_serve(cred: dict, reserve_percent: float, release_within_s: float, now: float) -> bool:
    """Mirrors what the proxy + quota-balancer would accept for this account right now."""
    if cred.get("disabled") or cred.get("unavailable"):
        return False
    retry = parse_ts(cred.get("next_retry_after"))
    if retry and retry > now:
        return False
    signals = (cred.get("quota") or {}).get("signals") or {}
    get = lambda k: signals.get(f"Anthropic-Ratelimit-Unified-{k}")  # noqa: E731
    for w in ("5h", "7d"):
        try:
            reset = float(get(f"{w}-Reset") or 0)
        except ValueError:
            reset = 0
        if reset <= now:
            continue  # window already refilled; old numbers no longer apply
        if str(get(f"{w}-Status") or "").lower() == "rejected":
            return False
        try:
            used = float(get(f"{w}-Utilization")) * 100
        except (TypeError, ValueError):
            continue
        if used >= 100:
            return False
        if used >= 100 - reserve_percent and reset - now > release_within_s:
            return False
    return True


@dataclass
class Account:
    name: str
    auth_index: str
    pinned: bool
    usage: dict  # raw /api/oauth/usage body


@dataclass
class Spend:
    rule: str  # "restore" | "salvage"
    account: Account
    grant_id: str
    why: str
    attempt_key: str = ""


@dataclass
class Plan:
    spends: list[Spend] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def grant_view(acc: Account, keep_resets: int, now: float):
    """The grant Anthropic would spend next, or (None, reason)."""
    ce = acc.usage.get("cedar_ember") or {}
    if ce.get("eligible") is not True:
        return None, "ineligible"
    cd = parse_ts(ce.get("cooldown_until"))
    if cd and cd > now:
        return None, "anthropic-cooldown"
    grant = next((g for g in ce.get("grants") or [] if g.get("id") == ce.get("next_grant_id")), None)
    if not grant:
        return None, "no-grant-left"
    if grant.get("paused") or grant.get("usable_now") is not True or (grant.get("resets_left") or 0) < 1:
        return None, "grant-not-usable"
    ends = parse_ts(grant.get("ends_at"))
    if ends is not None and ends <= now:
        return None, "grant-expired"
    imminent = ends is not None and ends - now <= IMMINENT_EXPIRY_S
    if not imminent and grant["resets_left"] - keep_resets < 1:
        return None, "keep-resets"
    if grant.get("use_requires_limit", True) and not ce.get("at_limit"):
        return None, "needs-limit"
    return grant, ""


WEEK_S = 7 * 86400


def worth_now(wait_s: float, burn_s: float) -> float:
    """Seconds of nonstop use a reset buys now: usage goes to 0, the end date stays."""
    return min(wait_s, burn_s)


def worth_waiting(wait_s: float, burn_s: float, expires_in_s: float | None) -> float:
    """Seconds of nonstop use the same grant buys if it is kept instead."""
    if expires_in_s is None:
        expires_in_s = float("inf")
    if expires_in_s <= wait_s:
        # Still blocked when it expires: the last-hour salvage resets it then.
        return min(burn_s, wait_s - expires_in_s)
    if expires_in_s >= wait_s + burn_s:
        # Refills, burns a full week, gets blocked with WEEK - B left: reset worth that.
        return min(burn_s, WEEK_S - burn_s)
    # Refills, then is still mid-week at expiry: salvage gives back what was used.
    used_s = expires_in_s - wait_s
    return min(used_s, wait_s + WEEK_S - expires_in_s)


def attempt_key(acc: Account, grant: dict) -> str:
    return f"{acc.auth_index}|{grant['id']}|{grant.get('resets_left')}"


def plan(accounts: list[Account], s: Settings, now: float, salvage_due: bool, attempted: set[str],
         burn_s: float | None = None) -> Plan:
    burn_s = burn_s or s.burn_hours * 3600
    out = Plan()
    for acc in accounts:
        weekly = acc.usage.get("seven_day") or {}
        ce = acc.usage.get("cedar_ember") or {}
        if (weekly.get("utilization") or 0) < 99.9 and "seven_day" not in (ce.get("exhausted") or []):
            continue  # weekly not used up: never reset, even if the 5-hour window is
        grant, why = grant_view(acc, s.keep_resets, now)
        if not grant:
            out.skipped.append(f"restore {acc.name}: {why}")
            continue
        if "seven_day" not in (grant.get("clears") or []):
            out.skipped.append(f"restore {acc.name}: grant does not clear the weekly window")
            continue
        wait_s = (parse_ts(weekly.get("resets_at")) or 0) - now
        if wait_s <= 0:
            continue
        ends = parse_ts(grant.get("ends_at"))
        now_s, later_s = worth_now(wait_s, burn_s), worth_waiting(wait_s, burn_s, None if ends is None else ends - now)
        detail = (f"refills in {wait_s / 3600:.1f} h; reset now buys {now_s / 3600:.1f} h, "
                  f"waiting buys {later_s / 3600:.1f} h (burn time {burn_s / 3600:.0f} h)")
        if now_s < later_s:
            out.skipped.append(f"restore {acc.name}: waiting, {detail}")
            continue
        key = attempt_key(acc, grant)
        if key in attempted:
            out.skipped.append(f"restore {acc.name}: already tried this grant")
            continue
        out.spends.append(Spend("restore", acc, grant["id"], f"weekly 100%, {detail}", key))
    if salvage_due:
        for acc in accounts:
            if any(sp.account is acc for sp in out.spends):
                continue
            grant, why = grant_view(acc, s.keep_resets, now)
            if not grant:
                continue
            ends = parse_ts(grant.get("ends_at"))
            if ends is None or ends - now > s.salvage_horizon_hours * 3600:
                continue
            imminent = ends - now <= IMMINENT_EXPIRY_S
            clears = set(grant.get("clears") or []) & set(WINDOWS)
            fullest = max(((acc.usage.get(w) or {}).get("utilization") or 0 for w in clears), default=0)
            if not imminent and fullest < s.salvage_min_used_percent:
                out.skipped.append(f"salvage {acc.name}: only {fullest:.0f}% used")
                continue
            key = attempt_key(acc, grant)
            if key in attempted:
                continue
            out.spends.append(Spend("salvage", acc, grant["id"],
                                    f"grant expires in {(ends - now) / 3600:.1f} h, {fullest:.0f}% used", key))
    return out


def weekly_full(cred: dict, now: float) -> bool:
    """The proxy's last seen weekly numbers for this account say it is used up."""
    signals = (cred.get("quota") or {}).get("signals") or {}
    get = lambda k: signals.get(f"Anthropic-Ratelimit-Unified-7d-{k}")  # noqa: E731
    try:
        if float(get("Reset") or 0) <= now:
            return False
        return str(get("Status") or "").lower() == "rejected" or float(get("Utilization") or 0) >= 0.999
    except ValueError:
        return False


def _summary(a: Account) -> str:
    ce = a.usage.get("cedar_ember") or {}
    g = next((g for g in ce.get("grants") or [] if g.get("id") == ce.get("next_grant_id")), None)
    left = g.get("resets_left") if g else 0
    u = lambda w: (a.usage.get(w) or {}).get("utilization")  # noqa: E731
    return f"{a.name}: 5h {u('five_hour')}% 7d {u('seven_day')}% resets_left {left}{' PINNED' if a.pinned else ''}"


# ---------------------------------------------------------------- proxy I/O


class Proxy:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key

    def _req(self, method: str, path: str, body=None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def credentials(self) -> list[dict]:
        d = self._req("GET", "/v8/management/credentials")
        return d.get("files") or d.get("credentials") or []

    def balancer(self) -> dict:
        try:
            return self._req("GET", "/v8/management/config/plugins/configs/quota-balancer") or {}
        except urllib.error.HTTPError:
            return {}

    def anthropic(self, auth_index: str, method: str, path: str, body=None, timeout: float = 25):
        payload = {"authIndex": auth_index, "method": method, "url": ANTHROPIC + path,
                   "header": dict(CLAUDE_HEADERS)}
        if body is not None:
            payload["data"] = json.dumps(body)
        r = self._req("POST", "/v8/management/requests/api-call", payload, timeout=timeout + 5)
        b = r.get("body")
        if isinstance(b, str):
            try:
                b = json.loads(b)
            except ValueError:
                pass
        return int(r.get("status_code") or 0), b

    def clear_cooldown(self, auth_index: str) -> None:
        self._req("POST", "/v0/management/reset-quota", {"auth_index": auth_index})


def duration_s(raw: str, default: float) -> float:
    raw = (raw or "").strip()
    units = {"h": 3600, "m": 60, "s": 1}
    try:
        return float(raw[:-1]) * units[raw[-1]] if raw and raw[-1] in units else float(raw or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- loop


class Keeper:
    def __init__(self, proxy: Proxy, s: Settings, state_path: str):
        self.p, self.s, self.state_path = proxy, s, state_path
        self.state = {"attempted": [], "last_salvage_at": 0, "spends": []}
        if os.path.exists(state_path):
            with open(state_path) as f:
                self.state.update(json.load(f))
        self.samples: dict[str, tuple] = {}
        self.burn_saved_at = 0.0
        self.last_read: dict[str, float] = {}
        self.usage_cache: dict[str, dict] = {}
        self.read_at: dict[str, float] = {}
        self.backoff: dict[str, float] = {}
        self.profiles: dict[str, dict] = {}
        self.profile_at: dict[str, float] = {}
        self.state.setdefault("auto", s.enabled)
        self.lock = threading.Lock()
        self.view: dict = {"accounts": [], "updated_at": None}

    @property
    def auto(self) -> bool:
        return bool(self.state.get("auto"))

    def set_auto(self, auto: bool) -> None:
        self.state["auto"] = auto
        self.save()
        self.log("mode", auto=auto)

    def log(self, event: str, **kw):
        print(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **kw}),
              flush=True)

    def save(self):
        with self.lock:
            self._save()

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def read_due(self, creds: list[dict], now: float) -> None:
        """Reads at most one account: the stalest one that is due and not backing off."""
        def due(c: dict) -> bool:
            idx = c["auth_index"]
            if now < self.backoff.get(idx, 0):
                return False
            age = now - self.last_read.get(idx, 0)
            if age >= REFRESH_S:
                return True
            # The proxy just saw this account hit its weekly limit: confirm it soon.
            return age >= READ_GAP_S and weekly_full(c, now) and not self.cached_full(c, now)
        candidates = sorted((c for c in creds if due(c)), key=lambda c: self.last_read.get(c["auth_index"], 0))
        if candidates:
            self.read_account(candidates[0], now)

    def read_account(self, cred: dict, now: float) -> dict | None:
        idx = cred["auth_index"]
        self.last_read[idx] = now
        code, body = self.p.anthropic(idx, "GET", USAGE_PATH, timeout=12)
        if code == 429:
            self.backoff[idx] = now + RATE_LIMIT_BACKOFF_S
        if code != 200 or not isinstance(body, dict):
            self.log("read-failed", account=cred["name"], status=code)
            return None
        self.usage_cache[idx] = body
        self.read_at[idx] = now
        if now - self.profile_at.get(idx, 0) >= PROFILE_REFRESH_S:
            pcode, profile = self.p.anthropic(idx, "GET", "/api/oauth/profile", timeout=12)
            if pcode == 200 and isinstance(profile, dict):
                self.profiles[idx], self.profile_at[idx] = profile, now
        return body

    def spend(self, sp: Spend) -> None:
        acc = sp.account
        self.log("spend", rule=sp.rule, account=acc.name, grant=sp.grant_id, why=sp.why, dry_run=self.s.dry_run)
        if self.s.dry_run:
            return
        self.state["attempted"].append(sp.attempt_key)
        self.save()
        code, prof = self.p.anthropic(acc.auth_index, "GET", "/api/oauth/profile", timeout=12)
        org = ((prof or {}).get("organization") or {}).get("uuid") if code == 200 else None
        if not org:
            self.state["attempted"].remove(sp.attempt_key)  # nothing was sent; may retry later
            self.save()
            self.log("spend-aborted", account=acc.name, reason=f"profile read failed ({code})")
            return
        result = "unknown"
        try:
            code, body = self.p.anthropic(acc.auth_index, "POST", f"/api/organizations/{org}/reset_rate_limits",
                                          {"program": "cedar_ember", "grant_id": sp.grant_id,
                                           "request_id": uuid.uuid4().hex})
            result = (body or {}).get("result", f"http {code}") if isinstance(body, dict) else f"http {code}"
        except Exception as e:  # the claim may still have landed; attempted key blocks a resend
            result = f"unknown ({e.__class__.__name__})"
        # Refusals prove nothing was spent, so the same grant may be tried again later.
        # An unknown outcome keeps its key: never risk spending twice.
        if result in ("not_limited", "cooldown", "ineligible", "unavailable", "http 429", "http 401", "http 403"):
            self.state["attempted"].remove(sp.attempt_key)
        cleared = False
        if result in ("reset", "already_used", "not_limited"):
            try:
                self.p.clear_cooldown(acc.auth_index)
                cleared = True
            except Exception as e:
                self.log("cooldown-clear-failed", account=acc.name, error=str(e))
        self.state["spends"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "rule": sp.rule, "account": acc.name, "grant": sp.grant_id,
                                     "why": sp.why, "result": result, "proxy_cooldown_cleared": cleared})
        self.save()
        self.usage_cache.pop(acc.auth_index, None)
        self.log("spent", account=acc.name, result=result, proxy_cooldown_cleared=cleared)

    def tick(self) -> None:
        now = time.time()
        creds = [c for c in self.p.credentials()
                 if c.get("provider") == "claude" and not c.get("disabled") and c.get("auth_index")]
        if not creds:
            return
        bal = self.p.balancer()
        reserve = float(bal.get("reserve-percent", 10)) if bal.get("enabled", True) else 0.0
        release = duration_s(str(bal.get("release-within", "1h")), 3600)
        serving = [c["name"] for c in creds if account_can_serve(c, reserve, release, now)]
        if sorted(serving) != getattr(self, "last_serving", None):
            self.last_serving = sorted(serving)
            self.log("pool", serving=len(serving), of=len(creds), reserve_percent=reserve,
                     accounts=[n.removeprefix("claude-").removesuffix(".json") for n in sorted(serving)])
        self.learn(creds)
        self.read_due(creds, now)
        salvage_due = now - self.state["last_salvage_at"] >= self.s.salvage_every_minutes * 60
        if salvage_due:
            self.state["last_salvage_at"] = now
            self.save()
        top = max((c.get("priority") or 0) for c in creds)
        accounts = [Account(c["name"].removeprefix("claude-").removesuffix(".json"), c["auth_index"],
                            top > 0 and (c.get("priority") or 0) == top, self.usage_cache[c["auth_index"]])
                    for c in creds if c["auth_index"] in self.usage_cache]
        result = plan(accounts, self.s, now, salvage_due, set(self.state["attempted"]), self.burn_s())
        if salvage_due:
            b = self.state.get("burn") or {}
            self.log("accounts", burn_hours=round(self.burn_s() / 3600, 1),
                     burn_learned=b.get("five_hour", 0) >= 0.5, status=[_summary(a) for a in accounts])
        if result.skipped != getattr(self, "last_skipped", None):
            self.last_skipped = result.skipped
            if result.skipped:
                self.log("waiting", skipped=result.skipped)
        self.update_view(accounts, result, now)
        for sp in result.spends:
            if self.auto:
                self.spend(sp)
            elif sp.attempt_key not in getattr(self, "would_spend", set()):
                self.would_spend = getattr(self, "would_spend", set()) | {sp.attempt_key}
                self.log("would-spend (manual mode)", rule=sp.rule, account=sp.account.name, why=sp.why)

    def learn(self, creds: list[dict]) -> None:
        """Adds up how much weekly usage each bit of 5-hour usage costs, within one window."""
        b = self.state.setdefault("burn", {"five_hour": 0.0, "seven_day": 0.0})
        for c in creds:
            sig = (c.get("quota") or {}).get("signals") or {}
            try:
                cur = tuple(float(sig.get(f"Anthropic-Ratelimit-Unified-{k}") or "nan")
                            for k in ("5h-Utilization", "7d-Utilization", "5h-Reset", "7d-Reset"))
            except ValueError:
                continue
            prev = self.samples.get(c["auth_index"])
            self.samples[c["auth_index"]] = cur
            if not prev or prev[2:] != cur[2:]:
                continue  # first sample, or a window rolled over in between
            d5, d7 = cur[0] - prev[0], cur[1] - prev[1]
            if d5 > 0 and d7 >= 0:
                b["five_hour"] += d5
                b["seven_day"] += d7
        if time.time() - self.burn_saved_at > 300:
            self.burn_saved_at = time.time()
            self.save()

    def burn_s(self) -> float:
        """Hours to burn a full week nonstop = 5 h per full window x windows per week."""
        b = self.state.get("burn") or {}
        if b.get("five_hour", 0) < 0.5 or b.get("seven_day", 0) <= 0:
            return self.s.burn_hours * 3600  # not enough traffic seen yet
        return max(5 * 3600, min(WEEK_S, 5 * 3600 * b["five_hour"] / b["seven_day"]))

    def cached_full(self, cred: dict, now: float) -> bool:
        """Anthropic's last answer said this account's weekly window is used up and not yet refilled."""
        week = (self.usage_cache.get(cred["auth_index"]) or {}).get("seven_day") or {}
        return (week.get("utilization") or 0) >= 99.9 and (parse_ts(week.get("resets_at")) or 0) > now

    def update_view(self, accounts: list[Account], result: Plan, now: float) -> None:
        """What the dashboard shows: every account read, with the keeper's reasoning."""
        notes = {}
        for line in result.skipped:
            rule, rest = line.split(" ", 1)
            name, why = rest.split(": ", 1)
            notes[name] = why
        for sp in result.spends:
            notes[sp.account.name] = ("resetting: " if self.auto else "would reset (manual mode): ") + sp.why
        rows = {r["name"]: r for r in self.view["accounts"]}
        for a in accounts:
            ce = a.usage.get("cedar_ember") or {}
            g = next((g for g in ce.get("grants") or [] if g.get("id") == ce.get("next_grant_id")), None)
            five, week = a.usage.get("five_hour") or {}, a.usage.get("seven_day") or {}
            rows[a.name] = {
                "name": a.name, "auth_index": a.auth_index,
                "five_hour_percent": five.get("utilization"), "five_hour_resets_at": five.get("resets_at"),
                "weekly_percent": week.get("utilization"), "weekly_resets_at": week.get("resets_at"),
                "resets_left": (g or {}).get("resets_left", 0), "reset_expires_at": (g or {}).get("ends_at"),
                "note": notes.get(a.name) or ("no reset left" if not g else
                                              "weekly not used up: no reset needed"
                                              if (week.get("utilization") or 0) < 99.9 else "checking"),
                "read_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            }
        self.view = {"accounts": sorted(rows.values(), key=lambda r: r["name"]),
                     "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}

    def status(self) -> dict:
        b = self.state.get("burn") or {}
        return {"auto": self.auto, "burn_hours": round(self.burn_s() / 3600, 1),
                "burn_learned": b.get("five_hour", 0) >= 0.5, "spends": self.state.get("spends", [])[-20:],
                **self.view}

    def usage(self) -> dict:
        """The keeper's latest Anthropic usage answer per account (the dashboard reads this)."""
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        return {"accounts": {idx: {"read_at": iso(self.read_at[idx]), "usage": body,
                                   "profile": self.profiles.get(idx)}
                             for idx, body in self.usage_cache.items() if idx in self.read_at}}

    def serve(self, key: str, port: int) -> None:
        keeper = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body=None):
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def _authorized(self) -> bool:
                given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                return bool(key) and hmac.compare_digest(given.encode(), key.encode())

            def do_OPTIONS(self):
                self._send(204)

            def do_GET(self):
                if self.path not in ("/status", "/usage"):
                    return self._send(404, {"error": "not found"})
                if not self._authorized():
                    return self._send(401, {"error": "management key required"})
                self._send(200, keeper.status() if self.path == "/status" else keeper.usage())

            def do_PUT(self):
                if self.path != "/mode":
                    return self._send(404, {"error": "not found"})
                if not self._authorized():
                    return self._send(401, {"error": "management key required"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    auto = body["auto"]
                    if not isinstance(auto, bool):
                        raise ValueError
                except (ValueError, KeyError):
                    return self._send(400, {"error": 'body must be {"auto": true|false}'})
                keeper.set_auto(auto)
                self._send(200, keeper.status())

            def log_message(self, *args):  # keep container logs to keeper events
                pass

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(self) -> None:
        self.log("start", auto=self.auto, settings=self.s.__dict__)
        while True:
            try:
                self.tick()
            except Exception as e:
                self.log("tick-error", error=f"{e.__class__.__name__}: {e}")
            time.sleep(self.s.check_every_s)


if __name__ == "__main__":
    key = os.environ.get("CPA_MGMT_KEY", "")
    if not key:
        sys.exit("CPA_MGMT_KEY is required")
    keeper = Keeper(Proxy(os.environ.get("CPA_URL", "http://cli-proxy-api:8317"), key), Settings(),
                    os.environ.get("STATE_FILE", "/state/state.json"))
    keeper.serve(key, int(env_num("PORT", 8318)))
    keeper.run()
