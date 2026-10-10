import copy,unittest
import native_solana as native
from test_pipeline import b58encode,MINT
CFG={'pool':'pool','program':native.AMM_V4,'mint':MINT,'quoteMint':native.SOL,'baseDecimals':6,'quoteDecimals':9,'baseVault':'base','quoteVault':'quote'}
def fixture(command=16):
    def bal(i,m,d,a):return {'accountIndex':i,'mint':m,'uiTokenAmount':{'decimals':d,'amount':str(a)}}
    return {'version':1,'slot':100,'blockTime':1710000000,'transaction':{'message':{'accountKeys':[{'pubkey':k} for k in ['pool','base','quote']], 'instructions':[{'programId':native.AMM_V4,'accounts':['pool','base','quote'],'data':b58encode(bytes([command])+bytes(16))}]}},'meta':{'err':None,'innerInstructions':[],'preTokenBalances':[bal(1,MINT,6,100000000),bal(2,native.SOL,9,10000000000)],'postTokenBalances':[bal(1,MINT,6,90000000),bal(2,native.SOL,9,11000000000)]}}
class Decoder(unittest.TestCase):
    def test_legacy_and_v2_swaps(self):
        for command in (9,11,16,17):
            rows,reason=native.decode(fixture(command),CFG,'sig');self.assertEqual(reason,'decoded');self.assertAlmostEqual(rows[0]['price'],.1);self.assertEqual(rows[0]['volume'],1);self.assertEqual(rows[0]['baseVolume'],10)
    def test_failed_transaction_not_price(self):
        tx=fixture();tx['meta']['err']={'InstructionError':[1,'x']};self.assertEqual(native.decode(tx,CFG,'sig')[0],[])
    def test_deposit_not_price(self):self.assertEqual(native.decode(fixture(3),CFG,'sig')[0],[])
    def test_multiple_swaps_are_ambiguous(self):
        tx=fixture();tx['meta']['innerInstructions']=[{'instructions':copy.deepcopy(tx['transaction']['message']['instructions'])}];self.assertEqual(native.decode(tx,CFG,'sig')[1],'unsupported_or_ambiguous')
    def test_vault_mint_mismatch_rejected(self):
        tx=fixture();tx['meta']['postTokenBalances'][0]['mint']='other';self.assertEqual(native.decode(tx,CFG,'sig')[0],[])
    def test_liquidity_same_direction_rejected(self):
        tx=fixture();tx['meta']['postTokenBalances'][0]['uiTokenAmount']['amount']='110000000';self.assertEqual(native.decode(tx,CFG,'sig')[0],[])
    def test_missing_block_time_not_fabricated(self):
        tx=fixture();tx['blockTime']=None;self.assertEqual(native.decode(tx,CFG,'sig')[0],[])
if __name__=='__main__':unittest.main()
