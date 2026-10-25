#!/usr/bin/env node
/**
 * Arb Fast Feed consumer (Offchain Labs PGA Fast Feed).
 * Early-paint only: decode successful tx logs → optional Uniswap V3 Swap hints.
 * tip/hot remains source of truth — do not replace charts SoT with this stream.
 *
 * Env:
 *   FEED_URL (default wss://arb1-txfeed.arbitrum.io/)
 *   API_KEY_FILE (required; path to the Fast Feed API key)
 *   OUT_DIR / OUT_JSONL (optional output paths)
 */
import fs from 'fs';
import path from 'path';
import WebSocket from 'ws';
import { createHash } from 'crypto';

const FEED_URL = process.env.FEED_URL || 'wss://arb1-txfeed.arbitrum.io/';
const API_KEY_FILE = process.env.API_KEY_FILE;
if (!API_KEY_FILE) throw new Error('set API_KEY_FILE');
const OUT_DIR = process.env.OUT_DIR || './out';
const OUT_JSONL = process.env.OUT_JSONL || path.join(OUT_DIR, 'quotes.jsonl');
const STATUS = path.join(OUT_DIR, 'status.json');

// Uniswap V3 Swap(address,address,int256,int256,uint160,uint128,int24)
const SWAP_TOPIC0 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67';

fs.mkdirSync(OUT_DIR, { recursive: true });
const API_KEY = fs.readFileSync(API_KEY_FILE, 'utf8').trim();
if (!API_KEY) throw new Error('empty API key');

let msgs = 0, swaps = 0, lastTs = 0;
function writeStatus(extra = {}) {
  fs.writeFileSync(STATUS, JSON.stringify({
    at: new Date().toISOString(),
    feed: FEED_URL,
    msgs, swaps, lastTs,
    role: 'early_paint_only',
    sot: 'tip_hot',
    ...extra,
  }) + '\n');
}

function handleMsg(raw) {
  msgs++;
  let msg;
  try { msg = JSON.parse(raw.toString()); } catch { return; }
  lastTs = msg.timestamp_ms || Date.now();
  const tx = msg.transaction || {};
  const receipt = tx.receipt || {};
  if (receipt.status === 0 || receipt.status === '0x0') return;
  const logs = receipt.logs || [];
  for (const log of logs) {
    const t0 = (log.topics && log.topics[0] || '').toLowerCase();
    if (t0 !== SWAP_TOPIC0) continue;
    swaps++;
    const row = {
      t_ms: lastTs,
      pga_round: msg.pga_round,
      block: tx.block_number,
      tx: tx.tx_hash,
      pool: log.address,
      // data decode left to price binder; keep raw for hot path
      data: log.data,
      topics: log.topics,
      early: true,
    };
    fs.appendFileSync(OUT_JSONL, JSON.stringify(row) + '\n');
  }
  if (msgs % 200 === 0) writeStatus();
}

function connect() {
  writeStatus({ state: 'connecting' });
  const ws = new WebSocket(FEED_URL, {
    headers: { Authorization: `Bearer ${API_KEY}` },
    handshakeTimeout: 4500,
  });
  ws.on('unexpected-response', (req, res) => {
    writeStatus({ state: 'rejected', http: res.statusCode });
    console.error(`Rejected HTTP ${res.statusCode} (401=no ticket yet; 429=conn limit)`);
    req.destroy();
    setTimeout(connect, 15_000);
  });
  ws.on('open', () => {
    writeStatus({ state: 'open' });
    console.error('Fast Feed open');
  });
  ws.on('message', (data) => {
    // keep read loop light
    setImmediate(() => handleMsg(data));
  });
  ws.on('close', (code, reason) => {
    writeStatus({ state: 'closed', code, reason: reason?.toString?.() });
    console.error('closed', code, reason?.toString?.());
    setTimeout(connect, 3_000);
  });
  ws.on('error', (e) => {
    writeStatus({ state: 'error', err: String(e.message || e) });
    console.error('error', e.message || e);
  });
}

connect();
setInterval(() => writeStatus({ state: 'heartbeat' }), 10_000);
