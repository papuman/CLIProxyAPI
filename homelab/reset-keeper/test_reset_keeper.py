"""Run: python3 -m unittest test_reset_keeper (from this folder)."""

import unittest
from datetime import datetime, timezone

from reset_keeper import Account, Settings, account_can_serve, plan, weekly_full, worth_now, worth_waiting

NOW = 1_800_000_000.0


def iso(offset_s: float) -> str:
    return datetime.fromtimestamp(NOW + offset_s, timezone.utc).isoformat()


def usage(five=0.0, seven=0.0, five_in=3 * 3600, seven_in=3 * 86400, resets_left=1, ends_in=14 * 86400,
          requires_limit=False, at_limit=False, cooldown=None):
    return {
        "five_hour": {"utilization": five, "resets_at": iso(five_in)},
        "seven_day": {"utilization": seven, "resets_at": iso(seven_in)},
        "cedar_ember": {
            "eligible": True, "at_limit": at_limit, "exhausted": [], "cooldown_until": cooldown,
            "next_grant_id": "g1" if resets_left else None,
            "grants": [{"id": "g1", "resets_left": resets_left, "resets_total": 1, "paused": False,
                        "usable_now": True, "use_requires_limit": requires_limit, "ends_at": iso(ends_in),
                        "clears": ["five_hour", "seven_day", "seven_day_overage_included"], "blocking": []}],
        },
    }


def acc(name, u, pinned=False):
    return Account(name, name + "-idx", pinned, u)


S = Settings(enabled=True, dry_run=False, burn_hours=50, keep_resets=0, salvage_horizon_hours=12,
             salvage_min_used_percent=25)


H = 3600
B = 50 * H  # burn time used in these tests


def restore(u, attempted=frozenset()):
    return plan([acc("a", u)], S, NOW, salvage_due=False, attempted=set(attempted), burn_s=B).spends


class Restore(unittest.TestCase):
    def test_five_hour_block_never_resets(self):
        self.assertEqual(restore(usage(five=100, seven=10)), [])

    def test_weekly_below_100_never_resets(self):
        self.assertEqual(restore(usage(five=100, seven=99)), [])

    def test_refill_further_than_burn_time_resets_now(self):
        self.assertEqual(len(restore(usage(seven=100, seven_in=72 * H, ends_in=14 * 86400))), 1)

    def test_refill_in_6h_waits_when_grant_has_time(self):
        # user's example: grant 2 days out; waiting + last-hour salvage buys ~42 h vs 6 h now
        self.assertEqual(restore(usage(seven=100, seven_in=6 * H, ends_in=48 * H)), [])
        self.assertEqual(restore(usage(seven=100, seven_in=6 * H, ends_in=14 * 86400)), [])

    def test_refill_in_6h_resets_when_grant_expires_soon(self):
        # grant expires in 8 h: now buys 6 h, waiting only ~2 h
        self.assertEqual(len(restore(usage(seven=100, seven_in=6 * H, ends_in=8 * H))), 1)

    def test_grant_expires_before_refill_resets_now(self):
        self.assertEqual(len(restore(usage(seven=100, seven_in=30 * H, ends_in=5 * H))), 1)

    def test_each_weekly_blocked_account_uses_its_own_reset(self):
        accounts = [acc("a", usage(seven=100, seven_in=80 * H)), acc("b", usage(seven=100, seven_in=90 * H)),
                    acc("c", usage(five=100))]
        names = [s.account.name for s in plan(accounts, S, NOW, False, set(), burn_s=B).spends]
        self.assertEqual(names, ["a", "b"])

    def test_no_grant_left_or_anthropic_cooldown_or_keep_or_repeat(self):
        far = dict(seven=100, seven_in=80 * H)
        self.assertEqual(restore(usage(**far, resets_left=0)), [])
        self.assertEqual(restore(usage(**far, cooldown=iso(600))), [])
        keep = Settings(**{**S.__dict__, "keep_resets": 1})
        self.assertEqual(plan([acc("a", usage(**far))], keep, NOW, False, set(), burn_s=B).spends, [])
        first = restore(usage(**far))[0]
        self.assertEqual(restore(usage(**far), {first.attempt_key}), [])


class Worth(unittest.TestCase):
    def test_user_examples(self):
        self.assertEqual(worth_now(6 * H, B), 6 * H)
        self.assertAlmostEqual(worth_waiting(6 * H, B, 48 * H) / H, 42)
        self.assertAlmostEqual(worth_waiting(6 * H, B, 8 * H) / H, 2)
        self.assertEqual(worth_waiting(6 * H, B, 14 * 86400), B)


class Salvage(unittest.TestCase):
    def test_spends_expiring_grant_when_used(self):
        p = plan([acc("a", usage(five=40, ends_in=6 * 3600))], S, NOW, True, set())
        self.assertEqual([s.rule for s in p.spends], ["salvage"])

    def test_keeps_far_or_unused(self):
        self.assertEqual(plan([acc("a", usage(five=40))], S, NOW, True, set()).spends, [])
        self.assertEqual(plan([acc("a", usage(five=10, ends_in=6 * 3600))], S, NOW, True, set()).spends, [])

    def test_imminent_expiry_spends_regardless(self):
        p = plan([acc("a", usage(five=1, ends_in=120))], S, NOW, True, set())
        self.assertEqual(len(p.spends), 1)


class Serve(unittest.TestCase):
    def cred(self, u5, u7, r5=NOW + 3600, r7=NOW + 86400 * 3, **kw):
        sig = {"Anthropic-Ratelimit-Unified-5h-Utilization": str(u5), "Anthropic-Ratelimit-Unified-5h-Reset": str(r5),
               "Anthropic-Ratelimit-Unified-7d-Utilization": str(u7), "Anthropic-Ratelimit-Unified-7d-Reset": str(r7)}
        return {"quota": {"signals": sig}, **kw}

    def test_reserve_and_release(self):
        self.assertTrue(account_can_serve(self.cred(0.5, 0.2), 5, 3600, NOW))
        self.assertFalse(account_can_serve(self.cred(0.2, 0.96), 5, 3600, NOW))
        self.assertTrue(account_can_serve(self.cred(0.2, 0.96, r7=NOW + 1800), 5, 3600, NOW))
        self.assertTrue(account_can_serve(self.cred(0.2, 0.96), 0, 3600, NOW))
        self.assertFalse(account_can_serve(self.cred(0.2, 0.2, unavailable=True), 5, 3600, NOW))

    def test_weekly_full_ignores_five_hour(self):
        self.assertFalse(weekly_full(self.cred(1.0, 0.3), NOW))
        self.assertTrue(weekly_full(self.cred(0.1, 1.0), NOW))
        self.assertFalse(weekly_full(self.cred(0.1, 1.0, r7=NOW - 10), NOW))


if __name__ == "__main__":
    unittest.main()


class Learn(unittest.TestCase):
    def test_burn_time_from_window_costs(self):
        import os
        import tempfile

        from reset_keeper import Keeper

        k = Keeper(None, S, os.path.join(tempfile.mkdtemp(), "state.json"))

        def cred(u5, u7, r5=NOW + 3600, r7=NOW + 86400):
            sig = {"Anthropic-Ratelimit-Unified-5h-Utilization": str(u5), "Anthropic-Ratelimit-Unified-5h-Reset": str(r5),
                   "Anthropic-Ratelimit-Unified-7d-Utilization": str(u7), "Anthropic-Ratelimit-Unified-7d-Reset": str(r7)}
            return {"auth_index": "x", "quota": {"signals": sig}}

        self.assertEqual(k.burn_s(), 50 * 3600)  # nothing seen yet: setting
        k.learn([cred(0.0, 0.10)])
        k.learn([cred(0.6, 0.16)])  # 60% of a 5h window cost 6% of the week
        k.learn([cred(0.0, 0.50, r5=NOW + 9999)])  # window rolled over: ignored
        self.assertAlmostEqual(k.burn_s() / 3600, 50)  # 10 windows x 5 h


class Http(unittest.TestCase):
    def test_status_needs_key_and_mode_switches(self):
        import json as _json
        import os
        import socket
        import tempfile
        import urllib.error
        import urllib.request

        from reset_keeper import Keeper

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        k = Keeper(None, S, os.path.join(tempfile.mkdtemp(), "state.json"))
        k.serve("secret", port)
        url = f"http://127.0.0.1:{port}"

        def call(path, method="GET", body=None, key="secret"):
            req = urllib.request.Request(url + path, method=method, data=_json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return _json.loads(r.read())

        with self.assertRaises(urllib.error.HTTPError) as e:
            call("/status", key="wrong")
        self.assertEqual(e.exception.code, 401)
        self.assertTrue(call("/status")["auto"])
        self.assertFalse(call("/mode", "PUT", {"auto": False})["auto"])
        self.assertFalse(Keeper(None, S, k.state_path).auto)  # survives restart
        with self.assertRaises(urllib.error.HTTPError) as e:
            call("/mode", "PUT", {"auto": "yes"})
        self.assertEqual(e.exception.code, 400)

    def test_manual_mode_never_spends(self):
        import os
        import tempfile
        import time as _time

        from reset_keeper import Keeper

        now = _time.time()
        u = usage(seven=100, seven_in=80 * H)
        for w in ("five_hour", "seven_day"):  # usage() is built around NOW; re-base on the real clock
            u[w]["resets_at"] = datetime.fromtimestamp(now + (80 * H if w == "seven_day" else 3 * H),
                                                       timezone.utc).isoformat()
        u["cedar_ember"]["grants"][0]["ends_at"] = datetime.fromtimestamp(now + 14 * 86400, timezone.utc).isoformat()
        calls = []

        class FakeProxy:
            def credentials(self):
                sig = {"Anthropic-Ratelimit-Unified-7d-Utilization": "1.0",
                       "Anthropic-Ratelimit-Unified-7d-Reset": str(now + 80 * H)}
                return [{"name": "claude-a.json", "provider": "claude", "auth_index": "a", "quota": {"signals": sig}}]

            def balancer(self):
                return {"reserve-percent": 0}

            def anthropic(self, idx, method, path, body=None, timeout=25):
                calls.append(method)
                return 200, u

        k = Keeper(FakeProxy(), S, os.path.join(tempfile.mkdtemp(), "state.json"))
        k.set_auto(False)
        k.tick()
        self.assertEqual(calls, ["GET"])  # read only, no claim POST
        self.assertIn("would reset (manual mode)", k.view["accounts"][0]["note"])
        self.assertEqual(k.state["attempted"], [])
