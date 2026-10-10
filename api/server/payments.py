"""Non-custodial USDC invoices payments. No wallet secret is used."""
import base64
import os
import struct
import time
import httpx
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.transaction import Transaction

RECIPIENT = os.environ.get('TICKING_SOLANA_RECIPIENT', '')
RPC_URL = os.environ.get('TICKING_SOLANA_RPC', 'https://api.mainnet-beta.solana.com')
MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
MEMO = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'
SYSTEM = '11111111111111111111111111111111'
SQUADS_V4 = 'SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf'
GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
from .products import PLANS


BILLING_PERIODS = {
    'monthly': {'term_days': 30, 'discount_percent': 0},
    'yearly': {'term_days': 365, 'discount_percent': 20},
}

def billing_quote(plan, period='monthly'):
    """Server-owned integer-cent prices, snapshotted on each invoice."""
    terms = BILLING_PERIODS[period]
    amount = PLANS[plan]['usd'] * 100
    if period == 'yearly': amount = amount * 12 * (100-terms['discount_percent']) // 100
    return {'amount': amount, **terms}

def plan_catalog():
    return {name: {**plan, 'billing': {period: billing_quote(name, period) for period in BILLING_PERIODS}}
            for name, plan in PLANS.items()}

class PaymentError(Exception):
    pass

class FailedTransaction(PaymentError):
    pass

def crypto_ready():
    try:
        if os.environ.get('TICKING_CRYPTO_ENABLED') != 'true':
            return False
        recipient=Pubkey.from_string(RECIPIENT)
        kind=os.environ.get('TICKING_RECIPIENT_KIND','wallet')
        if kind=='wallet':
            return recipient.is_on_curve()
        if kind=='squads-v4':
            # Receiving vaults do not sign. Bind the configured off-curve address
            # to the verified Squad and vault index instead of allowing any PDA.
            multisig=Pubkey.from_string(os.environ.get('TICKING_SQUADS_MULTISIG',''))
            index=int(os.environ.get('TICKING_SQUADS_VAULT_INDEX',''))
            if not 0<=index<=255:
                return False
            vault,_=Pubkey.find_program_address(
                [b'multisig',bytes(multisig),b'vault',bytes([index])],
                Pubkey.from_string(SQUADS_V4))
            return recipient==vault
        return False
    except (ValueError,TypeError):
        return False

def associated(owner):
    return Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(TOKEN)), bytes(Pubkey.from_string(MINT))], Pubkey.from_string(ATA))[0]

def rpc(method, params=None):
    try:
        response = httpx.post(RPC_URL, json={'jsonrpc': '2.0','id': 1,'method': method,'params': params or []}, timeout=15)
        response.raise_for_status()
        result = response.json()
        if result.get('error'):
            raise PaymentError('The Solana network is temporarily unavailable. Retry verification shortly.')
        return result.get('result')
    except (httpx.HTTPError, ValueError) as e:
        raise PaymentError('The Solana network is temporarily unavailable. No payment has been credited.') from e

def unsigned_transaction(invoice,rpc_fn=None):
    rpc=rpc_fn or globals()['rpc']
    if rpc('getGenesisHash') != GENESIS:
        raise PaymentError('Payment network configuration does not match Solana mainnet.')
    receiver=invoice.get('recipient') or RECIPIENT
    payer=Pubkey.from_string(invoice['payer']); recipient=Pubkey.from_string(receiver)
    source=associated(str(payer)); destination=associated(receiver)
    # Idempotent ATA creation and a checked, exact integer USDC transfer.
    create=Instruction(Pubkey.from_string(ATA), b'\x01', [AccountMeta(payer,True,True),AccountMeta(destination,False,True),AccountMeta(recipient,False,False),AccountMeta(Pubkey.from_string(MINT),False,False),AccountMeta(Pubkey.from_string(SYSTEM),False,False),AccountMeta(Pubkey.from_string(TOKEN),False,False)])
    transfer=Instruction(Pubkey.from_string(TOKEN), struct.pack('<BQB',12,invoice['amount']*10000,6), [AccountMeta(source,False,True),AccountMeta(Pubkey.from_string(MINT),False,False),AccountMeta(destination,False,True),AccountMeta(payer,True,False),AccountMeta(Pubkey.from_string(invoice['reference']),False,False)])
    memo=Instruction(Pubkey.from_string(MEMO), ('ticking:'+invoice['id']).encode(), [AccountMeta(payer,True,False)])
    latest=rpc('getLatestBlockhash',[{'commitment':'finalized'}])['value']
    message=Message.new_with_blockhash([create,transfer,memo],payer,Hash.from_string(latest['blockhash']))
    transaction=Transaction.new_unsigned(message)
    return {'transaction':base64.b64encode(bytes(transaction)).decode(),'last_valid_block_height':latest['lastValidBlockHeight']}

def validate_receipt(invoice, transaction, status):
    """Reject failed, unfinalized, wrong-token, wrong-sender and reused payments."""
    if status and status.get('err') is not None:
        raise FailedTransaction('The transaction failed on Solana. No payment has been credited. Create a new invoice to retry.')
    if not status or status.get('confirmationStatus') != 'finalized':
        return False
    if not transaction or transaction.get('meta',{}).get('err') is not None:
        raise PaymentError('This transaction did not execute successfully.')
    timestamp=transaction.get('blockTime')
    if not timestamp or timestamp < invoice['created']-30 or timestamp > invoice['expires']:
        raise PaymentError('The transaction falls outside this invoice’s payment window.')
    message=transaction['transaction']['message']; keys=message.get('accountKeys',[])
    addresses=[k['pubkey'] if isinstance(k,dict) else k for k in keys]
    if invoice['reference'] not in addresses:
        raise PaymentError('This transaction belongs to a different invoice.')
    signers=[k['pubkey'] for k in keys if isinstance(k,dict) and k.get('signer')]
    if invoice['payer'] not in signers:
        raise PaymentError('The payer does not match the invoice.')
    receiver=invoice.get('recipient') or RECIPIENT
    destination=str(associated(receiver)); expected=invoice['amount']*10000
    transfers=[]
    instructions=list(message.get('instructions',[]))
    for group in transaction['meta'].get('innerInstructions',[]) or []:
        instructions.extend(group.get('instructions',[]))
    for instruction in instructions:
        parsed=instruction.get('parsed',{})
        if not isinstance(parsed,dict) or instruction.get('programId')!=TOKEN or parsed.get('type')!='transferChecked':
            continue
        info=parsed.get('info',{})
        # RPC parsers label an authority with trailing reference accounts as
        # multisigAuthority. The actual payer must still be a message signer.
        authority=info.get('authority') or info.get('multisigAuthority')
        if info.get('mint')==MINT and info.get('destination')==destination and authority==invoice['payer']:
            amount=info.get('tokenAmount',{})
            if amount.get('decimals')==6:
                transfers.append(int(amount.get('amount','0')))
    if transfers != [expected]:
        raise PaymentError('The USDC amount, token, or recipient does not match this invoice.')
    index=addresses.index(destination) if destination in addresses else -1
    def amount_at(entries):
        values=[int(e['uiTokenAmount']['amount']) for e in entries if e.get('accountIndex')==index and e.get('mint')==MINT and e.get('owner')==receiver]
        return sum(values)
    after=amount_at(transaction['meta'].get('postTokenBalances',[])); before=amount_at(transaction['meta'].get('preTokenBalances',[]))
    if after-before != expected:
        raise PaymentError('The recipient’s confirmed USDC balance change does not match.')
    return True

def verify_invoice(invoice, signature,rpc_fn=None,evidence=False):
    rpc=rpc_fn or globals()['rpc']
    if rpc('getGenesisHash') != GENESIS:
        raise PaymentError('Payment network configuration does not match Solana mainnet.')
    status=rpc('getSignatureStatuses',[[signature],{'searchTransactionHistory':True}])['value'][0]
    if status and status.get('err') is not None:
        raise FailedTransaction('The transaction failed on Solana. No payment has been credited. Create a new invoice to retry.')
    if not status or status.get('confirmationStatus')!='finalized':
        return False
    tx=rpc('getTransaction',[signature,{'encoding':'jsonParsed','commitment':'finalized','maxSupportedTransactionVersion':0}])
    valid=validate_receipt(invoice,tx,status)
    if not valid or not evidence:return valid
    return {'signature':signature,'genesis':GENESIS,'status':status,'transaction':tx}
