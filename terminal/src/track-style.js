export default `.track-workspace,.track-retired{max-width:1180px;margin:0 auto;padding:28px 20px 48px;color:#1c2230;font:15px/1.45 "DM Sans",sans-serif}
.track-workspace-bar{display:flex;gap:16px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;margin-bottom:16px}
.track-kicker{margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7564c3}
.track-workspace h1,.track-retired h1{margin:0;font:500 28px/1.1 "Space Grotesk",sans-serif}
.track-workspace-bar p[role=status]{margin:0;max-width:520px;color:#5c6578;font-size:13px}
.track-ask{display:flex;gap:8px;align-items:flex-end;flex:1 1 320px;margin:0 0 14px}
.track-ask label{display:grid;gap:4px;flex:1;font-size:12px;color:#5c6578}
.track-ask input{border:1px solid #d9deea;border-radius:8px;padding:8px 10px;min-width:0;width:100%;font:inherit;background:#fff}
.track-caption{margin:6px 0 0;min-height:1.2em;color:#1c2230;font-size:12px;font-weight:600}
.track-workspace-bar button,.track-retired button,.track-side button,.track-ask button{border:1px solid #d9deea;background:#fff;border-radius:8px;padding:8px 12px;cursor:pointer;font:inherit}
.track-layout{display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
.track-layout:has(.track-side:not([hidden])){grid-template-columns:minmax(0,1fr) 200px}
.track-charts{display:grid;gap:12px}
.track-charts.is-multi{grid-template-columns:minmax(0,1.35fr) minmax(260px,.8fr)}
.track-pane{background:#fff;border:1px solid #e4e7f0;border-radius:12px;overflow:hidden}
.track-pane-enter{animation:track-pane-in .38s ease}
@keyframes track-pane-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.track-pane header{display:flex;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid #eef0f5}
.track-pane header small{color:#6d7588;font-size:11px}
.track-chart-host{position:relative;height:480px}
.track-side{border:1px solid #e4e7f0;border-radius:12px;padding:12px;background:#fff}
.track-side h2{margin:0 0 8px;font-size:13px}
.track-side button{display:grid;width:100%;text-align:left;margin-top:8px}
.track-side small{color:#6d7588;font-size:10px}
.track-paint-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2}
.track-paint-ui{position:absolute;inset:0;z-index:3;pointer-events:none}
.track-tag,.track-chip,.track-beside{position:absolute;pointer-events:auto;font:600 10px/1 "DM Sans",sans-serif}
.track-tag{transform:translateX(-50%);padding:2px 6px;border-radius:999px;background:#fff;border:1px solid currentColor;white-space:nowrap}
.track-tag.kind-buy{color:#239a82}.track-tag.kind-sell{color:#d36a79}.track-tag.kind-dump{color:#c2410c;background:#fff7ed}.track-tag.kind-transfer{color:#7564c3}.track-tag.kind-bridge{color:#3b82f6}.track-tag.kind-swap{color:#d97706}
.track-tag.sharp{z-index:2;padding:5px 9px;font-size:12px;color:#c2410c;background:#fff7ed;border-color:#c2410c;box-shadow:0 2px 8px rgba(194,65,12,.28);cursor:pointer}
.track-tag.sharp:hover,.track-tag.sharp:focus-visible{outline:2px solid #c2410c;outline-offset:2px;transform:translateX(-50%) scale(1.06)}
.track-tag.is-ahead,.track-chip.is-ahead,.track-beside.is-ahead{opacity:.28}
.track-tag.is-now,.track-chip.is-now{z-index:3;box-shadow:0 0 0 2px #111827}
.track-tag.is-now{transform:translateX(-50%) scale(1.08)}
.track-chip{transform:translateX(-50%);border-radius:999px;padding:5px 10px;background:#fff;cursor:pointer;border:1px dashed #98a0b3;color:#1c2230;box-shadow:0 1px 2px rgba(28,34,48,.08)}
.track-chip:hover,.track-chip:focus-visible{outline:2px solid #7564c3;outline-offset:2px;z-index:4;transform:translateX(-50%) translateY(-1px);box-shadow:0 4px 12px rgba(28,34,48,.16)}
.track-chip.solid{border-style:solid;background:#111827;color:#fff}
.track-chip.dashed{border-style:dashed;background:#fff}
.track-chip.bridge{transform:none;border-style:dashed;border-color:#2563eb;color:#1d4ed8;background:#eff6ff;font-weight:700}
.track-chip.bridge:hover,.track-chip.bridge:focus-visible,.track-chip.bridge.is-now{transform:translateY(-1px)}
.track-beside{right:12px;bottom:42px;left:auto;top:auto;border:0;border-radius:8px;background:#7564c3;color:#fff;padding:8px 10px;cursor:pointer;box-shadow:0 2px 8px rgba(117,100,195,.28)}
.track-beside:hover,.track-beside:focus-visible{outline:2px solid #111827;outline-offset:2px}
.track-readout{position:absolute;left:12px;bottom:12px;z-index:5;pointer-events:none;max-width:min(440px,72%);padding:6px 8px;border-radius:8px;background:rgba(17,24,39,.92);color:#fff;font:600 11px/1.35 "DM Sans",sans-serif}
@media(prefers-reduced-motion:reduce){.track-pane-enter{animation:none}}
.track-retired{padding-top:16px}
.track-retired h1{font-size:22px}
.track-retired form{display:flex;gap:8px;margin-top:8px;align-items:flex-end}
.track-retired label{display:grid;gap:4px;flex:1;font-size:12px;color:#5c6578}
.track-retired input{border:1px solid #d9deea;border-radius:8px;padding:8px 10px;min-width:0;width:100%;font:inherit}
@media(max-width:800px){.track-charts.is-multi,.track-layout:has(.track-side:not([hidden])){grid-template-columns:1fr}.track-chart-host{height:420px}}
`;
