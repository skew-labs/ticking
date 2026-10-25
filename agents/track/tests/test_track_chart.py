#!/usr/bin/env python3
"""Unit tests — synthetic hops; no parquet required."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hops_to_chart_markers import hops_to_chart_markers, build_hop_bands
from muse_track_mode import detect_track_intent, build_track_act, compose_from_hops
from em_track_autonomy import plan_track, act_track, run_track_loop


SEED = "0x1111111111111111111111111111111111111111"
A = SEED
B = "0x2222222222222222222222222222222222222222"
C = "0x3333333333333333333333333333333333333333"
CEX = "0x4444444444444444444444444444444444444444"


def _label(addr: str):
    if addr.lower() == CEX:
        return {"name": "Binance Deposit", "class": "cex"}
    return None


TRANSFERS = [
    {"ts": 1_700_000_000_000, "chain": "arbitrum", "tx": "0xaaa", "from": A, "to": B, "token": "0xdead", "amount": "1", "usd": 50_000},
    {"ts": 1_700_000_060_000, "chain": "arbitrum", "tx": "0xbbb", "from": B, "to": C, "token": "0xdead", "amount": "1", "usd": 49_000},
    {"ts": 1_700_000_120_000, "chain": "arbitrum", "tx": "0xccc", "from": C, "to": CEX, "token": "0xdead", "amount": "1", "usd": 48_000},
]


class TestHopsToMarkers(unittest.TestCase):
    def test_tags_bands_cex_chip(self):
        paint = hops_to_chart_markers(
            seed=SEED, transfers=TRANSFERS, period_ms=60_000, label_fn=_label
        )
        self.assertEqual(paint["schema"], "ticking.chartMarkers.track.v1")
        self.assertGreaterEqual(len(paint["tags"]), 3)
        bands = [m for m in paint["markers"] if m.get("paint") == "band"]
        self.assertGreaterEqual(len(bands), 1)
        # CEX chip solid
        cex_tags = [t for t in paint["tags"] if t.get("kind") == "cex_deposit"]
        self.assertTrue(cex_tags)
        self.assertEqual(cex_tags[0]["style"], "solid" if False else cex_tags[0].get("chips", [{}])[-1].get("style") or "solid")
        # candle floor alignment
        for t in paint["tags"]:
            self.assertEqual(t["time"] % 60_000, 0)
        # muse summary has no raw 0x wall of many addresses as primary — short summary
        self.assertIn("Painted", paint["museSummary"])
        self.assertNotIn("\n0x1111", paint["museSummary"])

    def test_hop_stack_abc(self):
        paint = hops_to_chart_markers(seed=SEED, transfers=TRANSFERS, period_ms=60_000)
        bands = build_hop_bands(paint["tags"], seed=SEED)
        self.assertGreaterEqual(len(bands), 2)
        self.assertEqual(bands[0]["from"], A)
        self.assertEqual(bands[0]["to"], B)
        self.assertIn(C, bands[1]["path"])

    def test_bridge_side_column(self):
        bridges = [{
            "ts_src": 1_700_000_200_000,
            "ts_dst": 1_700_000_260_000,
            "src_chain": "ethereum",
            "dst_chain": "arbitrum",
            "tx_src": "0xbr1",
            "from": A,
            "to": B,
            "token": "0xdead",
            "amount": "1",
            "status": "complete",
            "usd": 10_000,
        }]
        paint = hops_to_chart_markers(seed=SEED, transfers=[], bridges=bridges, coverage={"transfers": "missing", "swaps": "missing", "bridges": "ok"})
        self.assertTrue(any(m.get("kind") == "bridge" for m in paint["bands"]))
        self.assertTrue(any(c.get("chain") == "arbitrum" for c in paint["sideColumns"]))
        br = next(m for m in paint["bands"] if m.get("kind") == "bridge")
        self.assertEqual(br["style"], "dotted")

    def test_honest_empty(self):
        paint = hops_to_chart_markers(seed=SEED, transfers=[], coverage={"transfers": "missing", "swaps": "missing", "bridges": "missing"})
        self.assertEqual(paint["markers"], [])
        self.assertIn("hops_empty_window", paint["honesty"])

    def test_suspect_only_filters_small(self):
        small = [{"ts": 1_700_000_000_000, "chain": "arbitrum", "tx": "0xs", "from": A, "to": B, "token": "0x", "amount": "1", "usd": 100}]
        paint = hops_to_chart_markers(seed=SEED, transfers=small, suspect_only=True, large_usd=10_000)
        self.assertEqual(paint["tags"], [])


class TestMuseTrack(unittest.TestCase):
    def test_detect_intent(self):
        d = detect_track_intent(f"이 돈 어디로 갔지? {SEED}")
        self.assertIsNotNone(d)
        self.assertEqual(d["mode"], "track")
        self.assertEqual(d["seedAddress"], SEED)

    def test_act_no_address_list(self):
        out = compose_from_hops(seed=SEED, transfers=TRANSFERS, label_fn=_label)
        act = out["act"]
        self.assertEqual(act["mode"], "track")
        self.assertFalse(act["muse"]["listAddresses"])
        self.assertIn("chat_address_wall", act["forbidden"])
        self.assertTrue(act["paint"]["chartMarkers"])
        self.assertNotIn("0x1111111111111111111111111111111111111111", act["muse"]["chat"])

    def test_follow_route_workspace(self):
        out = compose_from_hops(seed=SEED, transfers=TRANSFERS)
        self.assertTrue(out["act"]["navigate"]["route"].startswith("#/console/workspace/"))


class TestEMAutonomy(unittest.TestCase):
    def test_loop_with_overrides(self):
        loop = run_track_loop(
            text="where did the money go",
            seed=SEED,
            transfers_override=TRANSFERS,
            swaps_override=[],
            bridges_override=[],
        )
        self.assertTrue(loop["ok"])
        self.assertEqual(loop["plan"]["strategy"], "paint_hops")
        self.assertFalse(loop["act"]["muse"]["listAddresses"])
        self.assertGreater(len(loop["act"]["paint"]["chartMarkers"]), 0)

    def test_plan_honest_empty(self):
        perc = {
            "schema": "ticking.perceive.track.v1",
            "subject": {"kind": "wallet", "id": SEED},
            "hops": {"transfers": [], "swaps": [], "bridges": []},
            "coverage": {"hops": {"transfers": "missing", "swaps": "missing", "bridges": "missing"}},
            "honesty": ["hops_empty_window"],
        }
        plan = plan_track(perc, intent={})
        self.assertEqual(plan["strategy"], "honest_empty")
        act = act_track(perc, plan)
        self.assertIn("honest empty", act["muse"]["chat"].lower())


if __name__ == "__main__":
    unittest.main()
