// node --test tests/test_networks.mjs  (frontend network helpers)
import test from 'node:test';import assert from 'node:assert/strict';
import {chainOf,networkParams,explorerUrl,evmAddress} from '../src/networks.js';
test('network selection',()=>{
 assert.equal(chainOf({network:'robinhood'}),'robinhood');assert.equal(chainOf({network:'exchange'}),'solana');assert.equal(chainOf(null),'solana');
 assert.deepEqual(networkParams({network:'arbitrum'}),{network:'arbitrum'});assert.deepEqual(networkParams({network:'solana'}),{});
 assert.ok(evmAddress.test('0x'+'ab'.repeat(20)));assert.ok(!evmAddress.test('0x'+'AB'.repeat(20)));
});
test('explorer links are exact and network-specific',()=>{
 assert.equal(explorerUrl('arbitrum','tx','0x'+'ab'.repeat(32)),'https://arbiscan.io/tx/0x'+'ab'.repeat(32));
 assert.equal(explorerUrl('robinhood','token','0x'+'cd'.repeat(20)),'https://robinhoodchain.blockscout.com/token/0x'+'cd'.repeat(20));
 assert.equal(explorerUrl('robinhood','address','0x'+'cd'.repeat(32)),null);   // V4 pool ids are not addresses
 assert.equal(explorerUrl('solana','tx','So11111111111111111111111111111111111111112'),'https://explorer.solana.com/tx/So11111111111111111111111111111111111111112');
 assert.equal(explorerUrl('arbitrum','tx','javascript:alert(1)'),null);
});
