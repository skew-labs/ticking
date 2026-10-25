"""Unit checks for the console side: exact network/address selection, review pinning, interval bounds, icon names.
usage (Canada host): cd fc/ui && TICKING_DB=/tmp/x.sqlite3 <.api-venv>/bin/python -m unittest ../src/tests/test_ui_evm.py"""
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
from fastapi import HTTPException  # noqa: E402
from server import evm_data, market_data, token_icons  # noqa: E402

NVDA = 'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec'


class UiTests(unittest.TestCase):
    def test_exact_identity_only(self):
        t = evm_data.chosen(NVDA)
        self.assertEqual((t['network'], t['symbol']), ('robinhood', 'NVDA'))
        self.assertIs(evm_data.chosen('robinhood:0xD0601CE157DB5BDC3162BBAC2A2C8AF5320D9EEC'), t)   # case-normalised address
        self.assertEqual(evm_data.chosen(network='robinhood', mint=t['mint']), t)
        self.assertIsNone(evm_data.chosen('NVDA'))                 # never a ticker
        self.assertIsNone(evm_data.chosen('arbitrum:' + t['mint']))  # never a guessed network
        self.assertIsNone(evm_data.chosen(NVDA, network='arbitrum'))

    def test_review_is_pinned_by_network(self):
        t = evm_data.chosen(NVDA)
        self.assertEqual(market_data.market_review(t)['status'], 'reviewed')
        self.assertIsNone(market_data.market_review({**t, 'network': 'arbitrum'}))
        self.assertTrue(market_data.market_review(next(x for x in market_data.CATALOG if x['id'] == 'SOL')))   # Solana unchanged

    def test_identity_view_is_static(self):
        row = evm_data.identity(NVDA)
        self.assertEqual((row['id'], row['network'], row['mint']), (NVDA, 'robinhood', NVDA.split(':')[1]))
        self.assertEqual((row['verification']['id'], row['verification']['mint']), (row['id'], row['mint']))   # passes reviewedMarket()
        self.assertIsNone(row['price'])
        self.assertRaises(HTTPException, evm_data.identity, 'arbitrum:' + NVDA.split(':')[1])

    def test_selection_and_bounds(self):
        self.assertRaises(HTTPException, evm_data.selection, None, 'arbitrum', '0x' + '00' * 20)
        self.assertEqual(evm_data.interval_seconds('15m', True), 900)
        self.assertRaises(HTTPException, evm_data.interval_seconds, '1s', True)   # token references are whole minutes
        self.assertEqual(evm_data.interval_seconds('1s', False), 1)

    def test_icon_names(self):
        self.assertTrue(token_icons.NETWORK_NAME.fullmatch('robinhood-0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec-de8d56d547449754.webp'))
        self.assertFalse(token_icons.NETWORK_NAME.fullmatch('robinhood-../../etc-de8d56d547449754.webp'))
        self.assertIsNone(token_icons.icon_url('0x' + '00' * 20, 'robinhood'))


if __name__ == '__main__':
    unittest.main()
