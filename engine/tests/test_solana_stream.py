import copy,json,tempfile,time,unittest
from pathlib import Path
from engine_runtime import EngineRuntime,EngineError
from native_solana import AMM_V4,SOL
from server import b58decode
from solana_stream import InputJournal,GeyserResultProcessor,b58encode,protobuf_modules,protobuf_receipt

POOL=b58encode(bytes([1])*32);BASE=b58encode(bytes([2])*32);QUOTE=b58encode(bytes([3])*32);MINT=b58encode(bytes([4])*32);SIGNATURE=b58encode(bytes([5])*64)
CFG={'pool':POOL,'program':AMM_V4,'mint':MINT,'quoteMint':SOL,'quote':'SOL','baseDecimals':6,'quoteDecimals':9,'baseVault':BASE,'quoteVault':QUOTE}
def block(slot=100,bank=7,hash='block-A',index=5):
    pb,_=protobuf_modules();u=pb.SubscribeUpdate();b=u.block;b.slot=slot;b.bank_id=bank;b.blockhash=hash;b.block_time.timestamp=1710000000
    info=b.transactions.add();info.signature=b58decode(SIGNATURE);info.index=index;info.transaction.signatures.append(info.signature)
    message=info.transaction.message;message.account_keys.extend(b58decode(k) for k in (POOL,BASE,QUOTE,AMM_V4))
    i=message.instructions.add();i.program_id_index=3;i.accounts=bytes([0,1,2]);i.data=bytes([16])+bytes(16)
    for name,amounts in [('pre_token_balances',(100000000,10000000000)),('post_token_balances',(90000000,11000000000))]:
        for idx,mint,decimals,amount in [(1,MINT,6,amounts[0]),(2,SOL,9,amounts[1])]:
            balance=getattr(info.meta,name).add();balance.account_index=idx;balance.mint=mint;balance.ui_token_amount.amount=str(amount);balance.ui_token_amount.decimals=decimals
    return u
def dead(slot=100,bank=7,with_bank=True):
    pb,_=protobuf_modules();u=pb.SubscribeUpdate();u.slot.slot=slot;u.slot.status=pb.SLOT_DEAD
    if with_bank:u.slot.bank_id=bank
    return u

