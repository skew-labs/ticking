// .releases/ticking-arkham-ta-20261020-01/.inputs/src/chart-continuity.js
var nonTimeCharts = /* @__PURE__ */ new Set(["renko", "linebreak", "kagi", "pnf", "range", "tick", "volume"]);
function candleBoundary(previous, current, period) {
  if (!previous || !current) return null;
  const from = previous.quoteCurrency, to = current.quoteCurrency;
  if (from && to && from !== to) return { kind: "quote", from: previous.time, to: current.time, fromQuote: from, toQuote: to };
  if (!nonTimeCharts.has(previous.chartTransform) && !nonTimeCharts.has(current.chartTransform) && Number.isFinite(period) && period > 0 && current.time - previous.time !== period) return { kind: "time", from: previous.time, to: current.time, missingMs: Math.max(0, current.time - previous.time - period) };
  return null;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/sdk/volume.js
var bases = /* @__PURE__ */ new Set(["solana_observed_pool_legs", "mapped_exchange_trades", "mixed_retained_sources", "pinned_venue_trades"]);
var number = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0;
function validVolumeObservation(v) {
  return !!v && v.policy === "independent_retained_volume_v1" && bases.has(v.basis) && number(v.baseVolume) && (v.quoteVolume === null || number(v.quoteVolume)) && Number.isSafeInteger(v.count) && v.count >= 0 && (Number.isSafeInteger(v.recordedMinutes) && v.recordedMinutes > 0 && v.recordedMinutes <= 10080 || v.recordedMinutes === null && v.basis === "pinned_venue_trades" && Number.isSafeInteger(v.sourcePeriodSeconds) && v.sourcePeriodSeconds >= 60 && v.sourcePeriodSeconds <= 604800) && v.complete === false && v.coverage === "retained_partial" && v.priceIndependent === true && Array.isArray(v.sources) && v.sources.length > 0 && v.sources.length <= 32 && v.sources.every((s) => typeof s === "string" && s.length <= 128) && Array.isArray(v.quoteCurrencies) && v.quoteCurrencies.length > 0 && v.quoteCurrencies.length <= 8 && v.quoteCurrencies.every((s) => typeof s === "string" && s.length <= 64);
}
function chartVolume(bar) {
  if (bar.synthetic) return { value: 0, available: false, label: "Volume unavailable \xB7 carried close" };
  const v = bar.derived ? null : bar.volumeObservation;
  if (validVolumeObservation(v)) return { value: v.baseVolume, available: true, label: v.basis === "solana_observed_pool_legs" ? "Retained Solana pool volume" : v.basis === "pinned_venue_trades" ? `Venue volume \xB7 ${v.sources.join(" + ")} \xB7 ${v.market || ""}` : v.basis === "mapped_exchange_trades" ? `Mapped venue volume \xB7 ${v.sources.join(" + ")}` : "Mixed retained volume sources" };
  if (!bar.referenceOnly && number(bar.volume)) return { value: bar.volume, available: bar.volume > 0 || bar.count > 0, label: "Recorded volume \xB7 retained coverage" };
  return { value: 0, available: false, label: "Volume unavailable \xB7 price observations only" };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/indicator-palette.js
var STUDY_COLORS = Object.freeze({ blue: "#397dcc", amber: "#b58324", teal: "#168f85", violet: "#9271c9", rose: "#ca6787", cyan: "#258bac", orange: "#cc783b" });
var { blue, amber, teal, violet, rose, cyan, orange } = STUDY_COLORS;
var groups = {
  amber: ["sma", "alma", "atr", "atrpct", "stddev", "mass", "tickvolume", "pivots"],
  blue: ["ema", "dema", "macd", "stochastic", "roc", "dpo", "bbpct", "pine", "ichimoku"],
  teal: ["rma", "vwma", "vwap", "supertrend", "mfi", "obv", "cmf", "aroon", "eom", "bullbear"],
  violet: ["wma", "linreg", "rsi", "stochrsi", "trix", "cmo", "expr", "tickmomentum"],
  rose: ["tema", "psar", "momentum", "force", "ac", "kst", "bbwidth"],
  cyan: ["hma", "donchian", "envelope", "cci", "adl", "ao", "ppo", "ultimate", "chaikin", "volosc", "tickregime", "bb"],
  orange: ["zlema", "keltner", "adx", "dmi", "williams", "tsi", "hv", "chaikinvol", "chop", "rvi", "fisher", "tickenvelope"]
};
var STUDY_PRIMARY = Object.freeze(Object.fromEntries(Object.entries(groups).flatMap(([name, ids]) => ids.map((id) => [id, STUDY_COLORS[name]]))));
var STUDY_LINES = Object.freeze({
  bb: { upper: cyan, middle: amber, lower: rose },
  vwap: { value: teal, upper: blue, lower: rose },
  donchian: { value: cyan, upper: blue, lower: rose },
  keltner: { value: orange, upper: blue, lower: rose },
  envelope: { value: cyan, upper: blue, lower: rose },
  tickenvelope: { value: orange, upper: blue, lower: rose },
  ichimoku: { value: blue, base: rose, upper: teal, lower: orange },
  pivots: { value: amber, r1: rose, s1: teal, r2: orange, s2: blue },
  macd: { value: blue, signal: orange },
  ppo: { value: cyan, signal: orange },
  adx: { value: orange, positive: teal, negative: rose },
  dmi: { value: orange, positive: teal, negative: rose },
  stochastic: { value: blue, signal: orange },
  stochrsi: { value: violet, signal: amber },
  kst: { value: rose, signal: cyan },
  tsi: { value: orange, signal: blue },
  aroon: { value: teal, signal: rose },
  bullbear: { value: teal, signal: rose },
  rvi: { value: orange, signal: blue },
  fisher: { value: orange, signal: blue }
});
var cycle = [amber, blue, teal, violet, rose, cyan, orange];
function studyPrimaryColor(config, configs = []) {
  if (config.color) return config.color;
  if (config.definition?.color) return config.definition.color;
  const base = STUDY_PRIMARY[config.id] || blue, index = configs.findIndex((s) => s === config || config.uid && s.uid === config.uid);
  const instance = index < 0 ? 0 : configs.slice(0, index).filter((s) => s.id === config.id).length;
  const secondary = new Set(Object.values(STUDY_LINES[config.id] || {}).slice(1)), choices = [base, ...cycle.filter((c) => c !== base && !secondary.has(c))];
  return choices[instance % choices.length];
}
var STUDY_HISTOGRAM = Object.freeze({ positive: teal, negative: rose });

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/pine-runtime.js
var PINE_EXAMPLE = `//@version=6
indicator("Ticking EMA + RSI", overlay=false)
length = input.int(14, "RSI length")
fast = ta.ema(close, 9)
slow = ta.ema(close, 21)
strength = ta.rsi(close, length)
plot(strength, "RSI")
plot(ta.sma(strength, 5), "RSI signal")
alertcondition(ta.crossover(fast, slow) and strength > 50, "Trend up")
alertcondition(ta.crossunder(fast, slow) and strength < 50, "Trend down")`;
var finite = (x) => typeof x === "number" && Number.isFinite(x);
var safeNames = /* @__PURE__ */ new Set(["open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "time", "bar_index", "true", "false", "na"]);
var calls = /* @__PURE__ */ new Set(["ta.sma", "ta.ema", "ta.rma", "ta.wma", "ta.rsi", "ta.highest", "ta.lowest", "ta.stdev", "ta.atr", "ta.change", "ta.crossover", "ta.crossunder", "math.abs", "math.sqrt", "math.log", "math.min", "math.max", "nz", "input.int", "input.float", "input.bool"]);
var forbidden = /* @__PURE__ */ new Set(["__proto__", "prototype", "constructor", "globalThis", "process", "window", "document", "fetch", "require", "eval", "Function"]);
var cache = /* @__PURE__ */ new Map();
function compilePine(source) {
  if (typeof source !== "string" || !source.trim() || source.length > 8192) throw Error("Pine source must contain 1\u20138,192 characters.");
  if (cache.has(source)) return cache.get(source);
  if (!/^\s*\/\/@version=[56]\s*$/m.test(source)) throw Error("Use //@version=5 or //@version=6.");
  const tokens = [], rx = /\s+|\/\/[^\n]*|"(?:\\["\\nrt]|[^"\\])*"|'(?:\\['\\nrt]|[^'\\])*'|\d+(?:\.\d+)?(?:e[+-]?\d+)?|\.[0-9]+|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?|>=|<=|==|!=|[()\[\],+\-*/%?:><=]/gy;
  let offset = 0, m;
  while (offset < source.length) {
    rx.lastIndex = offset;
    m = rx.exec(source);
    if (!m) throw Error("Unsupported Pine syntax near " + source.slice(offset, offset + 24));
    offset = rx.lastIndex;
    if (!/^\s|^\/\//.test(m[0])) tokens.push(m[0]);
    if (tokens.length > 1800) throw Error("Pine source is too complex.");
  }
  let cursor = 0, nodes = 0;
  const variables = new Set(safeNames), statements = [], plots = [], signals = [];
  const string = (t) => {
    if (!t || !/^['"]/.test(t)) throw Error("Use a literal title.");
    return t.slice(1, -1).replace(/\\(["'\\])/g, "$1").slice(0, 100);
  };
  function expression(depth = 0, min = 0) {
    if (depth > 24 || ++nodes > 700) throw Error("Pine expression is too complex.");
    let t = tokens[cursor++], a;
    if (["-", "+", "not"].includes(t)) a = { kind: "unary", op: t, value: expression(depth + 1, 7) };
    else if (t === "(") {
      a = expression(depth + 1);
      if (tokens[cursor++] !== ")") throw Error("Close every parenthesis.");
    } else if (t && /^(?:\d|\.)/.test(t)) a = { kind: "number", value: Number(t) };
    else if (t && /^['"]/.test(t)) a = { kind: "string", value: string(t) };
    else if (t && /^[A-Za-z_]/.test(t) && !forbidden.has(t)) {
      if (tokens[cursor] === "(") {
        if (!calls.has(t)) throw Error("Unsupported Pine function: " + t);
        cursor++;
        const args = [], named = {};
        while (tokens[cursor] !== ")") {
          if (tokens[cursor + 1] === "=") {
            const key = tokens[cursor++];
            cursor++;
            named[key] = expression(depth + 1);
          } else args.push(expression(depth + 1));
          if (tokens[cursor] !== ",") break;
          cursor++;
        }
        if (tokens[cursor++] !== ")") throw Error("Close every function.");
        a = { kind: "call", name: t, args, named };
      } else {
        if (!variables.has(t)) throw Error("Unknown Pine series: " + t);
        a = { kind: "variable", name: t };
      }
    } else throw Error("Expected a supported Pine expression.");
    if (tokens[cursor] === "[") {
      cursor++;
      const shift = Number(tokens[cursor++]);
      if (!Number.isInteger(shift) || shift < 0 || shift > 500 || tokens[cursor++] !== "]") throw Error("History offsets must be fixed integers from 0 to 500.");
      a = { kind: "lag", value: a, shift };
    }
    const precedence = { or: 1, and: 2, "==": 3, "!=": 3, ">": 3, "<": 3, ">=": 3, "<=": 3, "+": 4, "-": 4, "*": 5, "/": 5, "%": 5 };
    while (precedence[tokens[cursor]] >= min && precedence[tokens[cursor]] !== void 0) {
      const op = tokens[cursor++], right = expression(depth + 1, precedence[op] + 1);
      a = { kind: "binary", op, left: a, right };
    }
    if (min === 0 && tokens[cursor] === "?") {
      cursor++;
      const yes = expression(depth + 1);
      if (tokens[cursor++] !== ":") throw Error("A conditional needs both branches.");
      a = { kind: "choice", condition: a, yes, no: expression(depth + 1) };
    }
    return a;
  }
  function argumentsOf() {
    if (tokens[cursor++] !== "(") throw Error("Expected a function call.");
    const args = [], named = {};
    while (tokens[cursor] !== ")") {
      if (tokens[cursor + 1] === "=") {
        const name = tokens[cursor++];
        cursor++;
        named[name] = expression();
      } else args.push(expression());
      if (tokens[cursor] !== ",") break;
      cursor++;
    }
    if (tokens[cursor++] !== ")") throw Error("Close every function.");
    return { args, named };
  }
  let title = "Pine indicator", overlay = false, declarations = 0;
  while (cursor < tokens.length) {
    const t = tokens[cursor++];
    if (t === "indicator") {
      if (++declarations > 1 || statements.length || plots.length) throw Error("Use one indicator declaration before your series.");
      const { args, named } = argumentsOf();
      if (args.length !== 1 || args[0]?.kind !== "string") throw Error("Indicator needs one literal title.");
      if (named.overlay && !(named.overlay.kind === "variable" && ["true", "false"].includes(named.overlay.name))) throw Error("Use overlay=true or overlay=false.");
      if (named.shorttitle && named.shorttitle.kind !== "string") throw Error("Use a literal short title.");
      title = args[0].value;
      overlay = named.overlay?.kind === "variable" && named.overlay.name === "true";
      if (Object.keys(named).some((k) => k !== "overlay" && k !== "shorttitle")) throw Error("Unsupported indicator declaration option.");
    } else if (t === "plot" || t === "alertcondition") {
      const { args, named } = argumentsOf(), target = t === "plot" ? plots : signals;
      if (!args[0] || args[0].kind === "string") throw Error(t + " needs a numeric or boolean series.");
      const label = args[1]?.value || named.title?.value || `${t} ${target.length + 1}`;
      if (Object.keys(named).some((k) => !["title", "message"].includes(k))) throw Error("This Pine subset supports title/message; use ticking controls for visual styling.");
      if (args.length > (t === "plot" ? 2 : 3)) throw Error("Unsupported " + t + " option.");
      if (target.length >= (t === "plot" ? 4 : 8)) throw Error("Use at most four plots and eight alert conditions.");
      target.push({ expression: args[0], title: label });
    } else {
      if (!/^[A-Za-z_][\w]*$/.test(t) || forbidden.has(t) || variables.has(t) || tokens[cursor++] !== "=") throw Error("Supported Pine statements: indicator, unique series assignments, plot and alertcondition.");
      const value = expression();
      statements.push({ name: t, value });
      variables.add(t);
      if (statements.length > 60) throw Error("Use at most 60 series assignments.");
    }
  }
  if (!declarations || !plots.length) throw Error("Use one indicator() and at least one plot().");
  const result = { version: "ticking-pine-subset-v1", title, overlay, statements, plots, signals };
  cache.set(source, result);
  if (cache.size > 32) cache.delete(cache.keys().next().value);
  return result;
}
function runPine(program, inputs, functions) {
  const n = inputs.close.length, variables = { ...inputs }, constant = (value) => Array(n).fill(value), clean = (x) => typeof x === "boolean" ? Number(x) : finite(x) ? x : null;
  if (!n) return Object.fromEntries([...program.plots.map((_, i) => [i ? "plot" + (i + 1) : "value", []]), ...program.signals.map((_, i) => ["signal" + (i + 1), []])]);
  let start = 0;
  const segment = inputs.close.map((v, i) => {
    if (v === null) start = i + 1;
    return start;
  });
  const pair = (a, b, fn) => a.map((x, i) => x === null || b[i] === null ? null : clean(fn(x, b[i])));
  const size = (a) => {
    const value = a[0];
    if (!Number.isInteger(value) || value < 1 || value > 500 || a.some((x) => x !== value)) throw Error("Pine lengths must be fixed integers from 1 to 500.");
    return value;
  };
  function evaluate(node) {
    if (node.kind === "number") return constant(node.value);
    if (node.kind === "string") return constant(node.value);
    if (node.kind === "variable") {
      if (node.name === "true") return constant(1);
      if (node.name === "false") return constant(0);
      if (node.name === "na") return constant(null);
      return variables[node.name];
    }
    if (node.kind === "lag") {
      const v = evaluate(node.value);
      return v.map((_, i) => i - node.shift >= segment[i] ? v[i - node.shift] : null);
    }
    if (node.kind === "unary") {
      return evaluate(node.value).map((x) => x === null ? null : clean(node.op === "-" ? -x : node.op === "+" ? +x : !x));
    }
    if (node.kind === "choice") {
      const a = evaluate(node.condition), yes = evaluate(node.yes), no = evaluate(node.no);
      return a.map((v, i) => v === null ? null : v ? yes[i] : no[i]);
    }
    if (node.kind === "binary") {
      const a = evaluate(node.left), b = evaluate(node.right), ops = { "+": (x, y) => x + y, "-": (x, y) => x - y, "*": (x, y) => x * y, "/": (x, y) => y ? x / y : NaN, "%": (x, y) => y ? x % y : NaN, ">": (x, y) => x > y, "<": (x, y) => x < y, ">=": (x, y) => x >= y, "<=": (x, y) => x <= y, "==": (x, y) => x === y, "!=": (x, y) => x !== y, and: (x, y) => !!x && !!y, or: (x, y) => !!x || !!y };
      return pair(a, b, ops[node.op]);
    }
    const args = node.args.map(evaluate), name = node.name, arity = (k) => {
      if (args.length !== k) throw Error(name + " needs " + k + " arguments.");
    };
    if (!name.startsWith("input.") && Object.keys(node.named).length) throw Error("Named arguments are supported only for input bounds and titles.");
    if (name.startsWith("input.")) {
      if (args.length < 1 || !args[0].length) throw Error("Input needs a default.");
      const value = args[0][0];
      if (!finite(value) || args[0].some((x) => x !== value) || name === "input.int" && !Number.isInteger(value)) throw Error("Input defaults must be fixed numeric values.");
      for (const [k, v] of Object.entries(node.named)) {
        if (!["minval", "maxval", "step", "title"].includes(k)) throw Error("Unsupported input option.");
        const bound = evaluate(v)[0];
        if (k === "minval" && value < bound || k === "maxval" && value > bound) throw Error("Input default is outside its bounds.");
      }
      return args[0];
    }
    if (name === "nz") {
      if (args.length < 1 || args.length > 2) throw Error("nz needs one or two arguments.");
      return args[0].map((x, i) => x === null ? args[1]?.[i] ?? 0 : x);
    }
    if (["math.abs", "math.sqrt", "math.log"].includes(name)) {
      arity(1);
      return args[0].map((x) => x === null ? null : clean(Math[name.slice(5)](x)));
    }
    if (["math.min", "math.max"].includes(name)) {
      arity(2);
      return pair(args[0], args[1], Math[name.slice(5)]);
    }
    if (name === "ta.atr") {
      arity(1);
      return functions.atr(size(args[0]));
    }
    if (name === "ta.change") {
      arity(1);
      return args[0].map((x, i) => i > 0 && i - 1 >= segment[i] && x !== null && args[0][i - 1] !== null ? x - args[0][i - 1] : null);
    }
    if (name === "ta.crossover" || name === "ta.crossunder") {
      arity(2);
      return args[0].map((x, i) => !i || i - 1 < segment[i] || x === null || args[1][i] === null || args[0][i - 1] === null || args[1][i - 1] === null ? null : Number(name === "ta.crossover" ? x > args[1][i] && args[0][i - 1] <= args[1][i - 1] : x < args[1][i] && args[0][i - 1] >= args[1][i - 1]));
    }
    arity(2);
    return functions[name.slice(3)](args[0], size(args[1]));
  }
  for (const s of program.statements) variables[s.name] = evaluate(s.value);
  const lines = {};
  program.plots.forEach((p4, i) => lines[i ? "plot" + (i + 1) : "value"] = evaluate(p4.expression).map(clean));
  program.signals.forEach((p4, i) => lines["signal" + (i + 1)] = evaluate(p4.expression).map(clean));
  return lines;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/indicator-catalog.js
var length = (period = 20) => ({ defaults: { period }, fields: [["period", "Length", 2, 500]] });
var d = (id, name, short, { pane = false, color = STUDY_PRIMARY[id], period = 20, defaults, fields: fields2, range, guides, ohlcv = false, ...rest } = {}) => ({ id, name, short, pane, color, ...length(period), ...defaults ? { defaults } : {}, ...fields2 ? { fields: fields2 } : {}, range, guides, ohlcv, ...rest });
var INDICATORS = [
  d("sma", "Simple moving average", "SMA"),
  d("ema", "Exponential moving average", "EMA", {}),
  d("bb", "Bollinger Bands", "BB", { defaults: { period: 20, multiplier: 2 }, fields: [["period", "Length", 2, 500], ["multiplier", "Std. deviations", 0.1, 10, 0.1]] }),
  d("rsi", "Relative strength index", "RSI", { pane: true, period: 14, range: [0, 100], guides: [30, 70] }),
  d("macd", "Moving average convergence divergence", "MACD", { pane: true, defaults: { fast: 12, slow: 26, signal: 9 }, fields: [["fast", "Fast", 2, 250], ["slow", "Slow", 3, 500], ["signal", "Signal", 2, 250]] }),
  ...[["wma", "Weighted moving average", "WMA"], ["rma", "Wilder moving average", "RMA"], ["hma", "Hull moving average", "HMA"], ["dema", "Double EMA", "DEMA"], ["tema", "Triple EMA", "TEMA"], ["zlema", "Zero lag EMA", "ZLEMA"], ["linreg", "Linear regression", "LINREG"]].map(([id, name, short]) => d(id, name, short)),
  d("alma", "Arnaud Legoux moving average", "ALMA", { defaults: { period: 20, offset: 0.85, sigma: 6 }, fields: [["period", "Length", 2, 500], ["offset", "Offset", 0, 1, 0.01], ["sigma", "Sigma", 1, 20, 0.1]] }),
  d("vwma", "Volume weighted moving average", "VWMA", { ohlcv: true }),
  d("vwap", "UTC session VWAP", "VWAP", { ohlcv: true, defaults: { multiplier: 1 }, fields: [["multiplier", "Band deviations", 0, 5, 0.1]] }),
  d("donchian", "Donchian channels", "DC", { ohlcv: true }),
  d("keltner", "Keltner channels", "KC", { ohlcv: true, defaults: { period: 20, atr: 10, multiplier: 2 }, fields: [["period", "EMA length", 2, 500], ["atr", "ATR length", 2, 500], ["multiplier", "ATR multiplier", 0.1, 10, 0.1]] }),
  d("envelope", "Moving average envelopes", "ENV", { defaults: { period: 20, multiplier: 2 }, fields: [["period", "Length", 2, 500], ["multiplier", "Percent", 0.1, 50, 0.1]] }),
  d("ichimoku", "Ichimoku cloud", "ICH", { ohlcv: true, defaults: { conversion: 9, base: 26, span: 52 }, fields: [["conversion", "Conversion", 2, 250], ["base", "Base / displacement", 2, 250], ["span", "Leading B", 2, 500]] }),
  d("supertrend", "Supertrend", "ST", { ohlcv: true, defaults: { period: 10, multiplier: 3 }, fields: [["period", "ATR length", 2, 500], ["multiplier", "Multiplier", 0.1, 20, 0.1]] }),
  d("psar", "Parabolic stop and reverse", "SAR", { ohlcv: true, defaults: { step: 0.02, maximum: 0.2 }, fields: [["step", "Acceleration step", 0.01, 0.2, 0.01], ["maximum", "Maximum", 0.02, 1, 0.01]] }),
  d("pivots", "Previous UTC day pivot points", "PIV", { ohlcv: true, defaults: {}, fields: [] }),
  d("atr", "Average true range", "ATR", { pane: true, ohlcv: true, period: 14 }),
  d("atrpct", "Average true range percent", "ATR%", { pane: true, ohlcv: true, period: 14 }),
  d("adx", "Average directional index", "ADX", { pane: true, ohlcv: true, period: 14, range: [0, 100], guides: [25] }),
  d("dmi", "Directional movement index", "DMI", { pane: true, ohlcv: true, period: 14, range: [0, 100], guides: [25] }),
  d("cci", "Commodity channel index", "CCI", { pane: true, ohlcv: true, period: 20, guides: [-100, 0, 100] }),
  d("stochastic", "Stochastic oscillator", "STOCH", { pane: true, ohlcv: true, range: [0, 100], guides: [20, 80], defaults: { period: 14, smooth: 3, signal: 3 }, fields: [["period", "K length", 2, 500], ["smooth", "K smoothing", 1, 100], ["signal", "D smoothing", 1, 100]] }),
  d("stochrsi", "Stochastic RSI", "SRSI", { pane: true, range: [0, 100], guides: [20, 80], defaults: { period: 14, smooth: 3, signal: 3 }, fields: [["period", "RSI / stochastic length", 2, 500], ["smooth", "K smoothing", 1, 100], ["signal", "D smoothing", 1, 100]] }),
  d("williams", "Williams percent range", "%R", { pane: true, ohlcv: true, period: 14, range: [-100, 0], guides: [-80, -20] }),
  d("mfi", "Money flow index", "MFI", { pane: true, ohlcv: true, period: 14, range: [0, 100], guides: [20, 80] }),
  d("roc", "Rate of change", "ROC", { pane: true, period: 12, guides: [0] }),
  d("momentum", "Momentum", "MOM", { pane: true, period: 10, guides: [0] }),
  d("obv", "On balance volume", "OBV", { pane: true, ohlcv: true, defaults: {}, fields: [] }),
  d("adl", "Accumulation / distribution", "ADL", { pane: true, ohlcv: true, defaults: {}, fields: [] }),
  d("cmf", "Chaikin money flow", "CMF", { pane: true, ohlcv: true, guides: [0] }),
  d("force", "Force index", "FI", { pane: true, ohlcv: true, period: 13, guides: [0] }),
  d("ao", "Awesome oscillator", "AO", { pane: true, ohlcv: true, guides: [0], defaults: { fast: 5, slow: 34 }, fields: [["fast", "Fast SMA", 2, 250], ["slow", "Slow SMA", 3, 500]] }),
  d("ac", "Accelerator oscillator", "AC", { pane: true, ohlcv: true, guides: [0], defaults: {}, fields: [] }),
  d("trix", "Triple smoothed rate of change", "TRIX", { pane: true, period: 15, guides: [0] }),
  d("kst", "Know sure thing", "KST", { pane: true, guides: [0], defaults: { signal: 9 }, fields: [["signal", "Signal SMA", 2, 100]] }),
  d("cmo", "Chande momentum oscillator", "CMO", { pane: true, range: [-100, 100], guides: [-50, 0, 50] }),
  d("ppo", "Percentage price oscillator", "PPO", { pane: true, guides: [0], defaults: { fast: 12, slow: 26, signal: 9 }, fields: [["fast", "Fast EMA", 2, 250], ["slow", "Slow EMA", 3, 500], ["signal", "Signal EMA", 2, 250]] }),
  d("dpo", "Detrended price oscillator (unshifted)", "DPO", { pane: true, guides: [0] }),
  d("tsi", "True strength index", "TSI", { pane: true, range: [-100, 100], guides: [0], defaults: { fast: 13, slow: 25, signal: 7 }, fields: [["fast", "Fast EMA", 2, 250], ["slow", "Slow EMA", 3, 500], ["signal", "Signal EMA", 2, 100]] }),
  d("ultimate", "Ultimate oscillator", "UO", { pane: true, ohlcv: true, range: [0, 100], guides: [30, 70], defaults: {}, fields: [] }),
  d("stddev", "Standard deviation", "SD", { pane: true }),
  d("hv", "Historical volatility (365 UTC days)", "HV", { pane: true }),
  d("aroon", "Aroon", "AROON", { pane: true, ohlcv: true, range: [0, 100], guides: [30, 70], period: 25 }),
  d("bbpct", "Bollinger percent B", "%B", { pane: true, guides: [0, 1], defaults: { period: 20, multiplier: 2 }, fields: [["period", "Length", 2, 500], ["multiplier", "Std. deviations", 0.1, 10, 0.1]] }),
  d("bbwidth", "Bollinger bandwidth", "BBW", { pane: true, defaults: { period: 20, multiplier: 2 }, fields: [["period", "Length", 2, 500], ["multiplier", "Std. deviations", 0.1, 10, 0.1]] }),
  d("chaikin", "Chaikin oscillator", "CHO", { pane: true, ohlcv: true, guides: [0], defaults: { fast: 3, slow: 10 }, fields: [["fast", "Fast EMA", 2, 250], ["slow", "Slow EMA", 3, 500]] }),
  d("chaikinvol", "Chaikin volatility", "CHV", { pane: true, ohlcv: true, period: 10, guides: [0] }),
  d("volosc", "Volume oscillator", "VO", { pane: true, ohlcv: true, guides: [0], defaults: { fast: 5, slow: 10 }, fields: [["fast", "Fast EMA", 2, 250], ["slow", "Slow EMA", 3, 500]] }),
  d("bullbear", "Bull / bear power", "BBP", { pane: true, ohlcv: true, period: 13, guides: [0] }),
  d("chop", "Choppiness index", "CHOP", { pane: true, ohlcv: true, period: 14, range: [0, 100], guides: [38.2, 61.8] }),
  d("rvi", "Relative vigor index", "RVI", { pane: true, ohlcv: true, period: 10, guides: [0] }),
  d("eom", "Ease of movement", "EOM", { pane: true, ohlcv: true, period: 14, guides: [0] }),
  d("mass", "Mass index", "MASS", { pane: true, ohlcv: true, period: 25, guides: [27] }),
  d("fisher", "Fisher transform", "FISH", { pane: true, ohlcv: true, period: 10, guides: [0] }),
  d("expr", "Custom formula", "FORMULA", { pane: true, defaults: {}, fields: [], custom: true }),
  d("tickregime", "Ticking trend regime", "TREG", { pane: true, ohlcv: true, range: [-100, 100], guides: [-25, 0, 25], defaults: { fast: 9, slow: 21, period: 14 }, fields: [["fast", "Fast EMA", 2, 250], ["slow", "Slow EMA", 3, 500], ["period", "ATR / RSI length", 2, 500]] }),
  d("tickmomentum", "Ticking momentum pressure", "TMOM", { pane: true, range: [-100, 100], guides: [-30, 0, 30], period: 14 }),
  d("tickvolume", "Ticking relative execution volume", "TVOL", { pane: true, ohlcv: true, guides: [1, 2], period: 20 }),
  d("tickenvelope", "Ticking volatility envelope", "TENV", { ohlcv: true, defaults: { period: 20, multiplier: 2 }, fields: [["period", "EMA / ATR length", 2, 500], ["multiplier", "ATR deviations", 0.1, 10, 0.1]] }),
  d("pine", "Pine-style indicator", "PINE", { pane: true, ohlcv: true, defaults: {}, fields: [], pine: true })
];
var MAX_STUDIES = 25;
var SOURCES = ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4"];
function normalizeIndicators(input) {
  if (!Array.isArray(input)) return [];
  if (input.length > MAX_STUDIES) throw Error(`A chart supports up to ${MAX_STUDIES} studies.`);
  const seen = /* @__PURE__ */ new Set(), counts = {};
  return input.flatMap((value) => {
    const def = INDICATORS.find((d2) => d2.id === value?.id);
    if (!def) return [];
    const config = { id: def.id, uid: value.uid || `${def.id}-${counts[def.id] = (counts[def.id] || 0) + 1}` };
    if (typeof config.uid !== "string" || !/^[\w-]{1,80}$/.test(config.uid) || seen.has(config.uid)) throw Error("Each study needs a unique instance.");
    for (const [key, , min, max, step = 1] of def.fields) {
      const n = Number(value[key] ?? def.defaults[key]);
      if (!Number.isFinite(n) || n < min || n > max || Math.abs(n / step - Math.round(n / step)) > 1e-7) throw Error(`Invalid ${def.short} ${key}.`);
      config[key] = n;
    }
    if (config.fast >= config.slow) throw Error("Fast length must be smaller than slow length.");
    if (def.id === "psar" && config.step > config.maximum) throw Error("SAR step cannot exceed the maximum.");
    config.source = value.source || "close";
    if (!SOURCES.includes(config.source) && !seen.has(config.source)) throw Error("A study source must be a price or an earlier study.");
    if (def.ohlcv && !SOURCES.includes(config.source)) throw Error("This study requires recorded OHLCV, not another study.");
    if (def.custom) {
      config.expression = String(value.expression || "(close - sma(close, 20)) / sma(close, 20) * 100").slice(0, 1e3);
    }
    if (def.pine) {
      config.script = value.script ?? PINE_EXAMPLE;
      compilePine(config.script);
    }
    if (value.color && /^#[\da-f]{6}$/i.test(value.color)) config.color = value.color;
    if (value.lineColors && typeof value.lineColors === "object") config.lineColors = Object.fromEntries(Object.entries(value.lineColors).filter(([k, v]) => /^[a-zA-Z][a-zA-Z0-9]{0,30}$/.test(k) && typeof v === "string" && /^#[\da-f]{6}$/i.test(v)).slice(0, 12));
    seen.add(config.uid);
    return [config];
  });
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/study-formula.js
function evaluateFormula(expression, inputs, functions) {
  if (typeof expression !== "string" || expression.length > 1e3) throw Error("Formula must be 1\u20131,000 characters.");
  const tokens = expression.match(/\d+(?:\.\d+)?(?:e[+-]?\d+)?|\.[0-9]+|[a-zA-Z_][\w]*|[()+\-*/,]/gi) || [];
  if (tokens.join("") !== expression.replace(/\s+/g, "") || tokens.length > 240) throw Error("Use numbers, price inputs, arithmetic and supported study functions.");
  let cursor = 0, nodes = 0;
  const n = inputs.close.length, constant = (v) => Array(n).fill(v), safe = (v) => Number.isFinite(v) ? v : null;
  const unary = (a, fn) => a.map((v) => v === null ? null : safe(fn(v)));
  const binary = (a, b, fn) => a.map((v, i) => v === null || b[i] === null ? null : safe(fn(v, b[i])));
  const size = (a) => {
    const v = a[0];
    if (!Number.isInteger(v) || v < 1 || v > 500 || a.some((x) => x !== v)) throw Error("Function lengths must be fixed integers from 1 to 500.");
    return v;
  };
  function term(depth = 0) {
    if (depth > 24 || ++nodes > 120) throw Error("Formula is too complex.");
    const t = tokens[cursor++];
    if (t === "-") return unary(term(depth + 1), (x) => -x);
    if (t === "+") return term(depth + 1);
    if (t === "(") {
      const v = expr(depth + 1);
      if (tokens[cursor++] !== ")") throw Error("Close every parenthesis.");
      return v;
    }
    if (t && /^(?:\d|\.)/.test(t)) return constant(Number(t));
    if (!t || !/^\w+$/.test(t)) throw Error("Expected a price, number or function.");
    if (tokens[cursor] !== "(") {
      if (!Object.hasOwn(inputs, t)) throw Error(`Unknown input: ${t}.`);
      return inputs[t];
    }
    cursor++;
    const args = [];
    if (tokens[cursor] !== ")") {
      do {
        args.push(expr(depth + 1));
        if (tokens[cursor] !== ",") break;
        cursor++;
      } while (true);
    }
    if (tokens[cursor++] !== ")") throw Error("Close every function.");
    const arity = (count) => {
      if (args.length !== count) throw Error(`${t} needs ${count} arguments.`);
    };
    if (["abs", "sqrt", "log"].includes(t)) {
      arity(1);
      return unary(args[0], Math[t]);
    }
    if (["min", "max"].includes(t)) {
      arity(2);
      return binary(args[0], args[1], Math[t]);
    }
    if (["sma", "ema", "wma", "rma", "rsi", "highest", "lowest", "stdev"].includes(t)) {
      arity(2);
      return functions[t](args[0], size(args[1]));
    }
    if (t === "lag") {
      arity(2);
      const p4 = size(args[1]);
      return args[0].map((_, i) => i >= p4 ? args[0][i - p4] : null);
    }
    if (t === "change") {
      arity(1);
      return args[0].map((v, i) => i && v !== null && args[0][i - 1] !== null ? v - args[0][i - 1] : null);
    }
    if (["crossup", "crossdown"].includes(t)) {
      arity(2);
      return args[0].map((v, i) => !i || v === null || args[1][i] === null || args[0][i - 1] === null || args[1][i - 1] === null ? null : Number(t === "crossup" ? v > args[1][i] && args[0][i - 1] <= args[1][i - 1] : v < args[1][i] && args[0][i - 1] >= args[1][i - 1]));
    }
    throw Error(`Unsupported function: ${t}.`);
  }
  function product(depth) {
    let a = term(depth);
    while (["*", "/"].includes(tokens[cursor])) {
      const op = tokens[cursor++], b = term(depth);
      a = binary(a, b, op === "*" ? (x, y) => x * y : (x, y) => y === 0 ? NaN : x / y);
    }
    return a;
  }
  function expr(depth = 0) {
    let a = product(depth);
    while (["+", "-"].includes(tokens[cursor])) {
      const op = tokens[cursor++], b = product(depth);
      a = binary(a, b, op === "+" ? (x, y) => x + y : (x, y) => x - y);
    }
    return a;
  }
  const result = expr();
  if (cursor !== tokens.length) throw Error("Unexpected formula token.");
  return result;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/indicator-math.js
var valid = (x) => typeof x === "number" && Number.isFinite(x);
var nil = (n) => Array(n).fill(null);
function rolling(v, p4, deviation = true) {
  const mean = nil(v.length), sd = deviation ? nil(v.length) : null, ring = new Float64Array(p4);
  let size = 0, k = 0, sum2 = 0, center = 0, m2 = 0, rebase = 0;
  v.forEach((x, i) => {
    if (!valid(x)) {
      size = k = sum2 = center = m2 = rebase = 0;
      return;
    }
    if (size === p4) {
      const old = ring[k];
      sum2 -= old;
      if (deviation && p4 > 1) {
        const next = (p4 * center - old) / (p4 - 1);
        m2 -= (old - center) * (old - next);
        center = next;
      }
      size--;
    }
    ring[k] = x;
    k = (k + 1) % p4;
    sum2 += x;
    size++;
    if (deviation) {
      const delta2 = x - center;
      center += delta2 / size;
      m2 += delta2 * (x - center);
    }
    if (size === p4) {
      const m = sum2 / p4;
      mean[i] = m;
      if (deviation) {
        if (++rebase >= p4 || m2 < 0) {
          m2 = 0;
          for (let j = 0; j < p4; j++) m2 += (ring[j] - m) ** 2;
          center = m;
          rebase = 0;
        }
        sd[i] = Math.sqrt(Math.max(0, m2 / p4 + (center - m) ** 2));
      }
    }
  });
  return { mean, deviation: sd };
}
var sma = (v, p4) => rolling(v, p4, false).mean;
function ema(v, p4, wilder = false) {
  let count = 0, sum2 = 0, previous = null;
  const a = wilder ? 1 / p4 : 2 / (p4 + 1);
  return v.map((x) => {
    if (!valid(x)) {
      count = sum2 = 0;
      previous = null;
      return null;
    }
    if (previous === null) {
      sum2 += x;
      if (++count < p4) return null;
      previous = sum2 / p4;
    } else previous += a * (x - previous);
    return previous;
  });
}
var rma = (v, p4) => ema(v, p4, true);
function wma(v, p4) {
  const out = nil(v.length), ring = new Float64Array(p4);
  let sum2 = 0, weighted = 0, count = 0, k = 0;
  v.forEach((x, i) => {
    if (!valid(x)) {
      sum2 = weighted = count = k = 0;
      return;
    }
    if (count === p4) {
      weighted -= sum2;
      sum2 -= ring[k];
    } else count++;
    weighted += count * x;
    sum2 += x;
    ring[k] = x;
    k = (k + 1) % p4;
    if (count === p4) out[i] = weighted / (p4 * (p4 + 1) / 2);
  });
  return out;
}
function rsi(v, p4) {
  let prev = null, gain = 0, loss = 0, count = 0;
  return v.map((x) => {
    if (!valid(x)) {
      prev = null;
      gain = loss = count = 0;
      return null;
    }
    if (prev === null) {
      prev = x;
      return null;
    }
    const delta2 = x - prev;
    prev = x;
    const g = Math.max(0, delta2), l = Math.max(0, -delta2);
    if (count < p4) {
      gain += g;
      loss += l;
      if (++count < p4) return null;
      gain /= p4;
      loss /= p4;
    } else {
      gain = (gain * (p4 - 1) + g) / p4;
      loss = (loss * (p4 - 1) + l) / p4;
    }
    return loss === 0 ? gain === 0 ? 50 : 100 : 100 - 100 / (1 + gain / loss);
  });
}
var zip = (a, b, fn) => a.map((x, i) => valid(x) && valid(b[i]) ? finite2(fn(x, b[i])) : null);
var finite2 = (x) => valid(x) ? x : null;
var div = (a, b) => zip(a, b, (x, y) => y === 0 ? NaN : x / y);
var sub = (a, b) => zip(a, b, (x, y) => x - y);
var add = (a, b) => zip(a, b, (x, y) => x + y);
var mul = (a, b) => zip(a, b, (x, y) => x * y);
var scale = (a, k) => a.map((x) => valid(x) ? x * k : null);
var delta = (v) => v.map((x, i) => i && valid(x) && valid(v[i - 1]) ? x - v[i - 1] : null);
var lag = (v, p4) => {
  let count = 0;
  return v.map((x, i) => {
    count = valid(x) ? count + 1 : 0;
    return count > p4 ? v[i - p4] : null;
  });
};
var roc = (v, p4) => scale(div(sub(v, lag(v, p4)), lag(v, p4)), 100);
function extrema(v, p4, high = true) {
  const out = nil(v.length), q = [];
  let head = 0, count = 0;
  v.forEach((x, i) => {
    if (!valid(x)) {
      q.length = 0;
      head = count = 0;
      return;
    }
    count++;
    while (q.length > head && (high ? v[q.at(-1)] <= x : v[q.at(-1)] >= x)) q.pop();
    q.push(i);
    while (q[head] <= i - p4) head++;
    if (count >= p4) out[i] = v[q[head]];
    if (head > 512) {
      q.splice(0, head);
      head = 0;
    }
  });
  return out;
}
var sum = (v, p4) => scale(sma(v, p4), p4);
function accumulated(v) {
  let s = 0;
  return v.map((x) => {
    if (!valid(x)) {
      s = 0;
      return null;
    }
    return s += x;
  });
}
var midpoint = (hi, lo, p4) => scale(add(extrema(hi, p4), extrema(lo, p4, false)), 0.5);
function slope(v, p4) {
  const out = nil(v.length);
  let count = 0, k = 0, sy = 0, sxy = 0;
  const ring = new Float64Array(p4), sx = p4 * (p4 - 1) / 2, sxx = p4 * (p4 - 1) * (2 * p4 - 1) / 6;
  v.forEach((x, i) => {
    if (!valid(x)) {
      count = k = sy = sxy = 0;
      return;
    }
    if (count === p4) {
      sxy -= sy - ring[k];
      sy -= ring[k];
    } else count++;
    sxy += (count - 1) * x;
    sy += x;
    ring[k] = x;
    k = (k + 1) % p4;
    if (count === p4) {
      const b = (p4 * sxy - sx * sy) / (p4 * sxx - sx * sx);
      out[i] = (sy - b * sx) / p4 + b * (p4 - 1);
    }
  });
  return out;
}
function computeStudy(bars, c, source, period, displayVolume = null) {
  const n = bars.length, p4 = c.period;
  switch (c.id) {
    case "sma":
      return { value: sma(source, p4) };
    case "ema":
      return { value: ema(source, p4) };
    case "rma":
      return { value: rma(source, p4) };
    case "wma":
      return { value: wma(source, p4) };
    case "hma":
      return { value: wma(sub(scale(wma(source, Math.floor(p4 / 2)), 2), wma(source, p4)), Math.round(Math.sqrt(p4))) };
    case "dema": {
      const a = ema(source, p4);
      return { value: sub(scale(a, 2), ema(a, p4)) };
    }
    case "tema": {
      const a = ema(source, p4), b = ema(a, p4);
      return { value: add(sub(scale(a, 3), scale(b, 3)), ema(b, p4)) };
    }
    case "zlema":
      return { value: ema(add(source, sub(source, lag(source, Math.floor((p4 - 1) / 2)))), p4) };
    case "linreg":
      return { value: slope(source, p4) };
    case "alma": {
      const weights = Array.from({ length: p4 }, (_, i) => Math.exp(-((i - c.offset * (p4 - 1)) ** 2) / (2 * (p4 / c.sigma) ** 2))), total = weights.reduce((a, b) => a + b, 0), out = nil(n);
      let start = 0;
      source.forEach((x, i) => {
        if (!valid(x)) {
          start = i + 1;
          return;
        }
        if (i - start + 1 >= p4) {
          let value = 0;
          for (let j = 0; j < p4; j++) value += source[i - p4 + 1 + j] * weights[j];
          out[i] = value / total;
        }
      });
      return { value: out };
    }
    default:
      break;
  }
  const mask = (i) => valid(source[i]), field = (k) => bars.map((b, i) => mask(i) && valid(b[k]) ? b[k] : null), hi = field("high"), lo = field("low"), op = field("open"), cl = field("close"), v = bars.map((b, i) => displayVolume ? mask(i) && valid(displayVolume[i]) ? displayVolume[i] : null : mask(i) && !b.synthetic && !b.referenceOnly && !b.estimated && valid(b.volume) && b.volume >= 0 ? b.volume : null), typ = scale(add(add(hi, lo), cl), 1 / 3), hl = scale(add(hi, lo), 0.5);
  const tr = hi.map((h, i) => h === null || lo[i] === null ? null : i && valid(cl[i - 1]) ? Math.max(h - lo[i], Math.abs(h - cl[i - 1]), Math.abs(lo[i] - cl[i - 1])) : h - lo[i]);
  const atr = (length2) => rma(tr, length2);
  const money = hi.map((h, i) => h === null || lo[i] === null || cl[i] === null || v[i] === null ? null : h === lo[i] ? 0 : (cl[i] - lo[i] - (h - cl[i])) / (h - lo[i]) * v[i]);
  switch (c.id) {
    case "tickregime": {
      const spread = div(sub(ema(cl, c.fast), ema(cl, c.slow)), atr(p4)), strength = rsi(cl, p4);
      return { value: spread.map((x, i) => x === null || strength[i] === null ? null : Math.max(-100, Math.min(100, Math.tanh(x) * 60 + (strength[i] - 50) * 0.8))) };
    }
    case "tickmomentum": {
      const strength = rsi(source, p4), signal = ema(source, p4);
      return { value: strength.map((x, i) => x === null || signal[i] === null || !signal[i] ? null : Math.max(-100, Math.min(100, (x - 50) * 1.5 + Math.tanh((source[i] / signal[i] - 1) * 100) * 25))) };
    }
    case "tickvolume":
      return { value: div(v, sma(v, p4)) };
    case "tickenvelope": {
      const value = ema(cl, p4), width = scale(atr(p4), c.multiplier);
      return { value, upper: add(value, width), lower: sub(value, width) };
    }
    case "pine":
      return runPine(compilePine(c.script), { open: op, high: hi, low: lo, close: cl, volume: v, hl2: hl, hlc3: typ, ohlc4: scale(add(add(op, cl), add(hi, lo)), 0.25), time: bars.map((b) => b.time), bar_index: bars.map((_, i) => i) }, { sma, ema, rma, wma, rsi, highest: (v2, p5) => extrema(v2, p5, true), lowest: (v2, p5) => extrema(v2, p5, false), stdev: (v2, p5) => rolling(v2, p5).deviation, atr });
    case "vwma":
      return { value: div(sum(mul(source, v), p4), sum(v, p4)) };
    case "vwap": {
      let total = 0, weighted = 0, day = null, mean = 0, m2 = 0;
      const value = nil(n), upper = nil(n), lower = nil(n);
      typ.forEach((x, i) => {
        const nextDay = Math.floor(bars[i].time / 864e5);
        if (day !== nextDay || !valid(x) || v[i] === null) {
          total = weighted = mean = m2 = 0;
          day = nextDay;
        }
        if (!valid(x) || !valid(v[i])) return;
        const weight = v[i];
        if (weight > 0) {
          const old = total;
          total += weight;
          weighted += weight * x;
          const d2 = x - mean;
          mean += weight / total * d2;
          m2 += weight * d2 * (x - mean);
          if (!old) m2 = 0;
        }
        if (total) {
          value[i] = weighted / total;
          const sd = Math.sqrt(Math.max(0, m2 / total));
          upper[i] = value[i] + c.multiplier * sd;
          lower[i] = value[i] - c.multiplier * sd;
        }
      });
      return { value, upper, lower };
    }
    case "bb":
    case "bbpct":
    case "bbwidth": {
      const { mean, deviation } = rolling(source, p4), upper = add(mean, scale(deviation, c.multiplier)), lower = sub(mean, scale(deviation, c.multiplier));
      if (c.id === "bb") return { middle: mean, upper, lower };
      return { value: c.id === "bbpct" ? div(sub(source, lower), sub(upper, lower)) : scale(div(sub(upper, lower), mean), 100) };
    }
    case "donchian": {
      const upper = extrema(hi, p4), lower = extrema(lo, p4, false);
      return { value: scale(add(upper, lower), 0.5), upper, lower };
    }
    case "keltner": {
      const value = ema(source, p4), range = scale(atr(c.atr), c.multiplier);
      return { value, upper: add(value, range), lower: sub(value, range) };
    }
    case "envelope": {
      const value = sma(source, p4);
      return { value, upper: scale(value, 1 + c.multiplier / 100), lower: scale(value, 1 - c.multiplier / 100) };
    }
    case "ichimoku": {
      const value = midpoint(hi, lo, c.conversion), base = midpoint(hi, lo, c.base);
      return { value, base, upper: lag(scale(add(value, base), 0.5), c.base), lower: lag(midpoint(hi, lo, c.span), c.base) };
    }
    case "supertrend": {
      const a = atr(p4), value = nil(n);
      let upper = null, lower = null, trend = 1;
      for (let i = 0; i < n; i++) {
        if (a[i] === null || hl[i] === null) {
          upper = lower = null;
          trend = 1;
          continue;
        }
        const u = hl[i] + c.multiplier * a[i], l = hl[i] - c.multiplier * a[i], prev = cl[i - 1];
        const nextUpper = upper === null || u < upper || prev > upper ? u : upper, nextLower = lower === null || l > lower || prev < lower ? l : lower;
        if (upper !== null) {
          if (trend < 0 && cl[i] > nextUpper) trend = 1;
          else if (trend > 0 && cl[i] < nextLower) trend = -1;
        }
        upper = nextUpper;
        lower = nextLower;
        value[i] = trend > 0 ? lower : upper;
      }
      return { value };
    }
    case "psar": {
      const value = nil(n);
      let begin = 0, sar = null, ep = null, acc = c.step, up = true;
      for (let i = 0; i < n; i++) {
        if (hi[i] === null || lo[i] === null) {
          begin = i + 1;
          sar = ep = null;
          continue;
        }
        if (i - begin < 1) continue;
        if (sar === null) {
          up = cl[i] >= cl[i - 1];
          sar = up ? lo[i - 1] : hi[i - 1];
          ep = up ? Math.max(hi[i], hi[i - 1]) : Math.min(lo[i], lo[i - 1]);
          acc = c.step;
        } else {
          sar += acc * (ep - sar);
          sar = up ? Math.min(sar, lo[i - 1], lo[Math.max(begin, i - 2)]) : Math.max(sar, hi[i - 1], hi[Math.max(begin, i - 2)]);
          if (up ? lo[i] < sar : hi[i] > sar) {
            up = !up;
            sar = ep;
            ep = up ? hi[i] : lo[i];
            acc = c.step;
          } else if (up ? hi[i] > ep : lo[i] < ep) {
            ep = up ? hi[i] : lo[i];
            acc = Math.min(c.maximum, acc + c.step);
          }
        }
        value[i] = sar;
      }
      return { value };
    }
    case "pivots": {
      const value = nil(n), r1 = nil(n), s1 = nil(n), r2 = nil(n), s2 = nil(n);
      let current = null, previous = null;
      bars.forEach((b, i) => {
        if (!mask(i)) return;
        const day = Math.floor(b.time / 864e5);
        if (current?.day !== day) {
          previous = current?.day === day - 1 ? current : null;
          current = { day, high: b.high, low: b.low, close: b.close, start: b.time };
        } else {
          current.high = Math.max(current.high, b.high);
          current.low = Math.min(current.low, b.low);
          current.close = b.close;
        }
        if (previous && previous.start === previous.day * 864e5) {
          const mid = (previous.high + previous.low + previous.close) / 3;
          value[i] = mid;
          r1[i] = 2 * mid - previous.low;
          s1[i] = 2 * mid - previous.high;
          r2[i] = mid + previous.high - previous.low;
          s2[i] = mid - previous.high + previous.low;
        }
      });
      return { value, r1, s1, r2, s2 };
    }
    case "rsi":
      return { value: rsi(source, p4) };
    case "macd":
    case "ppo": {
      const a = ema(source, c.fast), b = ema(source, c.slow), value = c.id === "ppo" ? scale(div(sub(a, b), b), 100) : sub(a, b), signal = ema(value, c.signal);
      return { value, signal, histogram: sub(value, signal) };
    }
    case "atr":
      return { value: atr(p4) };
    case "atrpct":
      return { value: scale(div(atr(p4), cl), 100) };
    case "adx":
    case "dmi": {
      const dh = delta(hi), dl = scale(delta(lo), -1), plus = dh.map((x, i) => valid(x) && valid(dl[i]) ? x > dl[i] && x > 0 ? x : 0 : null), minus = dl.map((x, i) => valid(x) && valid(dh[i]) ? x > dh[i] && x > 0 ? x : 0 : null), trAligned = tr.map((x, i) => valid(dh[i]) ? x : null), range = rma(trAligned, p4), positive = scale(div(rma(plus, p4), range), 100), negative = scale(div(rma(minus, p4), range), 100), dx = zip(positive, negative, (a, b) => a + b === 0 ? 0 : 100 * Math.abs(a - b) / (a + b));
      return { value: rma(dx, p4), positive, negative };
    }
    case "cci": {
      const mean = sma(typ, p4), value = nil(n);
      for (let i = p4 - 1; i < n; i++) {
        if (mean[i] === null) continue;
        let deviation = 0;
        for (let j = i - p4 + 1; j <= i; j++) deviation += Math.abs(typ[j] - mean[i]);
        value[i] = deviation === 0 ? 0 : (typ[i] - mean[i]) / (0.015 * deviation / p4);
      }
      return { value };
    }
    case "stochastic":
    case "stochrsi":
    case "williams": {
      const base = c.id === "stochrsi" ? rsi(source, p4) : cl, high = extrema(c.id === "stochrsi" ? base : hi, p4), low = extrema(c.id === "stochrsi" ? base : lo, p4, false), raw = zip(sub(base, low), sub(high, low), (a, b) => b === 0 ? 50 : 100 * a / b);
      if (c.id === "williams") return { value: raw.map((x) => x === null ? null : x - 100) };
      const value = sma(raw, c.smooth);
      return { value, signal: sma(value, c.signal) };
    }
    case "mfi": {
      const change = delta(typ), flow = mul(typ, v), positive = flow.map((x, i) => valid(x) && valid(change[i]) ? change[i] > 0 ? x : 0 : null), negative = flow.map((x, i) => valid(x) && valid(change[i]) ? change[i] < 0 ? x : 0 : null);
      return { value: zip(sum(positive, p4), sum(negative, p4), (a, b) => b === 0 ? a === 0 ? 50 : 100 : 100 - 100 / (1 + a / b)) };
    }
    case "roc":
      return { value: roc(source, p4) };
    case "momentum":
      return { value: sub(source, lag(source, p4)) };
    case "obv": {
      const change = delta(cl);
      return { value: accumulated(v.map((x, i) => valid(x) ? !i || cl[i - 1] === null ? 0 : Math.sign(change[i]) * x : null)) };
    }
    case "adl":
      return { value: accumulated(money) };
    case "cmf":
      return { value: div(sum(money, p4), sum(v, p4)) };
    case "force":
      return { value: ema(mul(delta(cl), v), p4) };
    case "ao":
    case "ac": {
      const value = sub(sma(hl, c.fast || 5), sma(hl, c.slow || 34));
      return c.id === "ac" ? { value: sub(value, sma(value, 5)), histogram: sub(value, sma(value, 5)) } : { value, histogram: value };
    }
    case "trix":
      return { value: roc(ema(ema(ema(source, p4), p4), p4), 1) };
    case "kst": {
      const value = add(add(sma(roc(source, 10), 10), scale(sma(roc(source, 15), 10), 2)), add(scale(sma(roc(source, 20), 10), 3), scale(sma(roc(source, 30), 15), 4)));
      return { value, signal: sma(value, c.signal) };
    }
    case "cmo": {
      const changes = delta(source), gain = changes.map((x) => x === null ? null : Math.max(x, 0)), loss = changes.map((x) => x === null ? null : Math.max(-x, 0)), a = sum(gain, p4), b = sum(loss, p4);
      return { value: zip(a, b, (x, y) => x + y === 0 ? 0 : 100 * (x - y) / (x + y)) };
    }
    case "dpo":
      return { value: sub(lag(source, Math.floor(p4 / 2) + 1), sma(source, p4)) };
    case "tsi": {
      const changes = delta(source), value = scale(div(ema(ema(changes, c.slow), c.fast), ema(ema(changes.map((x) => x === null ? null : Math.abs(x)), c.slow), c.fast)), 100);
      return { value, signal: ema(value, c.signal) };
    }
    case "ultimate": {
      const bp = cl.map((x, i) => i && valid(x) && valid(cl[i - 1]) && valid(lo[i]) ? x - Math.min(lo[i], cl[i - 1]) : null), range = tr.map((x, i) => valid(bp[i]) ? x : null), a = div(sum(bp, 7), sum(range, 7)), b = div(sum(bp, 14), sum(range, 14)), d2 = div(sum(bp, 28), sum(range, 28));
      return { value: scale(add(add(scale(a, 4), scale(b, 2)), d2), 100 / 7) };
    }
    case "stddev":
      return { value: rolling(source, p4).deviation };
    case "hv": {
      const log = zip(source, lag(source, 1), (a, b) => a > 0 && b > 0 ? Math.log(a / b) : NaN);
      return { value: scale(rolling(log, p4).deviation, 100 * Math.sqrt(365 * 864e5 / period)) };
    }
    case "aroon": {
      const value = nil(n), signal = nil(n);
      let start = 0;
      hi.forEach((x, i) => {
        if (!valid(x) || !valid(lo[i])) {
          start = i + 1;
          return;
        }
        if (i - start < p4) return;
        let h = i, l = i;
        for (let j = i - p4; j <= i; j++) {
          if (hi[j] >= hi[h]) h = j;
          if (lo[j] <= lo[l]) l = j;
        }
        value[i] = 100 * (p4 - (i - h)) / p4;
        signal[i] = 100 * (p4 - (i - l)) / p4;
      });
      return { value, signal };
    }
    case "chaikin": {
      const a = accumulated(money);
      return { value: sub(ema(a, c.fast), ema(a, c.slow)) };
    }
    case "chaikinvol":
      return { value: roc(ema(sub(hi, lo), p4), p4) };
    case "volosc": {
      const slow = ema(v, c.slow);
      return { value: scale(div(sub(ema(v, c.fast), slow), slow), 100) };
    }
    case "bullbear": {
      const mean = ema(cl, p4);
      return { value: sub(hi, mean), signal: sub(lo, mean) };
    }
    case "chop": {
      const a = sum(tr, p4), range = sub(extrema(hi, p4), extrema(lo, p4, false));
      return { value: zip(a, range, (x, y) => x > 0 && y > 0 ? 100 * Math.log10(x / y) / Math.log10(p4) : NaN) };
    }
    case "rvi": {
      const smooth = (x) => x.map((v2, i) => i >= 3 && [v2, x[i - 1], x[i - 2], x[i - 3]].every(valid) ? (v2 + 2 * x[i - 1] + 2 * x[i - 2] + x[i - 3]) / 6 : null), value = div(sma(smooth(sub(cl, op)), p4), sma(smooth(sub(hi, lo)), p4));
      return { value, signal: smooth(value) };
    }
    case "eom":
      return { value: sma(scale(div(mul(delta(hl), sub(hi, lo)), v), 1e8), p4) };
    case "mass": {
      const first = ema(sub(hi, lo), 9);
      return { value: sum(div(first, ema(first, 9)), p4) };
    }
    case "fisher": {
      const high = extrema(hl, p4), low = extrema(hl, p4, false), value = nil(n), signal = nil(n);
      let previous = 0, fish = 0;
      for (let i = 0; i < n; i++) {
        if (high[i] === null || low[i] === null || hl[i] === null) {
          previous = fish = 0;
          continue;
        }
        const unit = high[i] === low[i] ? 0 : (hl[i] - low[i]) / (high[i] - low[i]) - 0.5;
        previous = Math.max(-0.999, Math.min(0.999, 0.66 * unit + 0.67 * previous));
        signal[i] = fish;
        fish = 0.5 * Math.log((1 + previous) / (1 - previous)) + 0.5 * fish;
        value[i] = fish;
      }
      return { value, signal };
    }
    case "expr":
      return { value: evaluateFormula(c.expression, { close: source, open: op, high: hi, low: lo, volume: v, hl2: hl, hlc3: typ, ohlc4: scale(add(add(op, cl), add(hi, lo)), 0.25) }, { sma, ema, wma, rma, rsi, highest: (v2, p5) => extrema(v2, p5), lowest: (v2, p5) => extrema(v2, p5, false), stdev: (v2, p5) => rolling(v2, p5).deviation }) };
    default:
      throw Error("Unknown native study.");
  }
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analysis-policy.js
var ANALYSIS_POLICY = "completed-source-v2";
var CALCULATION_VERSION = "sma-seed-wilder-v2";
function barIssues(b, quote) {
  const out = [];
  if (b.synthetic) out.push("carried_close");
  if (b.estimated) out.push("estimated_reference");
  if (b.sourceCandleState === "provisional") out.push("provisional_source");
  const units = b.quoteCurrencies;
  if (Array.isArray(units) && (units.length > 1 || units.length === 1 && units[0] !== quote) || b.mixedSource && (!Array.isArray(units) || units.length !== 1)) out.push("ambiguous_quote");
  return out;
}
function decisionInput(b, quote = b.quoteCurrency) {
  return barIssues(b, quote).length === 0;
}
function sourceSegment(bars, period, quote) {
  let start = 0;
  const exclusions = {};
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], issues = barIssues(b, b.quoteCurrency || quote);
    for (const issue of issues) exclusions[issue] = (exclusions[issue] || 0) + 1;
    if (issues.length) start = i + 1;
    else if (i && (b.time - bars[i - 1].time !== period || (b.quoteCurrency || quote) !== (bars[i - 1].quoteCurrency || quote))) start = i + 1;
  }
  return { bars: bars.slice(start), start, exclusions, policy: ANALYSIS_POLICY };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/indicators.js
var volumeStudies = /* @__PURE__ */ new Set(["vwma", "vwap", "mfi", "obv", "adl", "cmf", "force", "chaikin", "volosc", "eom", "tickvolume"]);
function computeIndicators(series, configs, period, { strict = false } = {}) {
  if (!configs.length) return [];
  const studies = [], outputs = /* @__PURE__ */ new Map();
  const displayGaps = strict ? null : series.map((b, i) => !!b.synthetic || !!(i && (series[i - 1].synthetic || candleBoundary(series[i - 1], b, period)?.kind === "time")));
  const displayVolume = strict ? null : series.map((b) => {
    const v = chartVolume(b);
    return v.available ? v.value : null;
  });
  const volumeMissing = strict ? null : displayVolume.map((v) => v === null);
  for (const config of configs) {
    const def = INDICATORS.find((d2) => d2.id === config.id);
    if (!def) continue;
    const prior = outputs.get(config.source), source = series.map((b, i) => {
      if (!Number.isFinite(b.close) || strict && (!decisionInput(b) || i && (period && b.time - series[i - 1].time !== period || b.quoteCurrency && series[i - 1].quoteCurrency && b.quoteCurrency !== series[i - 1].quoteCurrency))) return null;
      if (prior) return prior[i] ?? null;
      switch (config.source) {
        case "open":
          return b.open;
        case "high":
          return b.high;
        case "low":
          return b.low;
        case "hl2":
          return (b.high + b.low) / 2;
        case "hlc3":
          return (b.high + b.low + b.close) / 3;
        case "ohlc4":
          return (b.open + b.high + b.low + b.close) / 4;
        default:
          return b.close;
      }
    });
    let lines, error = null;
    try {
      lines = computeStudy(series, config, source, period, displayVolume);
    } catch (e) {
      error = e.message;
      lines = { value: Array(series.length).fill(null) };
    }
    if (strict && config.id === "pivots") {
      const dayMs = 864e5, days = /* @__PURE__ */ new Map();
      for (let i = 0; i < series.length; i++) {
        const b = series[i], day = Math.floor(b.time / dayMs), d2 = days.get(day) || { first: b.time, last: b.time - period, count: 0, valid: true };
        d2.valid &&= source[i] !== null && b.time === d2.last + period;
        d2.last = b.time;
        d2.count++;
        days.set(day, d2);
      }
      for (const key of Object.keys(lines)) lines[key] = lines[key].map((v, i) => {
        const day = Math.floor(series[i].time / dayMs) - 1, d2 = days.get(day);
        return period > 0 && dayMs % period === 0 && d2?.valid && d2.first === day * dayMs && d2.last + period === (day + 1) * dayMs && d2.count === dayMs / period ? v : null;
      });
    }
    if (strict && config.id === "vwap") {
      let day = null, complete = false;
      const available = series.map((b, i) => {
        const next = Math.floor(b.time / 864e5);
        if (next !== day) {
          day = next;
          complete = b.time === day * 864e5;
        }
        complete &&= source[i] !== null && !b.referenceOnly;
        return complete;
      });
      for (const key of Object.keys(lines)) lines[key] = lines[key].map((v, i) => available[i] ? v : null);
    }
    for (const key of Object.keys(lines)) lines[key] = lines[key].map((v) => typeof v === "number" && Number.isFinite(v) ? v : null);
    const pine = def.pine ? compilePine(config.script) : null;
    const dependency = studies.find((s) => s.uid === config.source), usesRecordedVolume = volumeStudies.has(config.id) || ["pine", "expr"].includes(config.id) && /\bvolume\b/.test(config.script || config.expression || "") || !!dependency?.usesRecordedVolume;
    outputs.set(config.uid, lines.value || lines.middle);
    studies.push({ ...config, definition: { ...def, pane: (pine ? !pine.overlay : def.pane) || !!dependency?.definition.pane, color: studyPrimaryColor(config, configs) }, lines, error, ...!strict ? { displayGaps, volumeMissing, usesRecordedVolume } : {} });
  }
  return studies;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/intervals.js
var MARKET_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "45m", "1h", "2h", "3h", "4h", "6h", "8h", "12h", "1d", "1w"];
var INTERVAL_PRESETS = ["1s", "3s", "5s", "15s", "30s", "45s", ...MARKET_INTERVALS];
var UNITS = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
function parseInterval(input) {
  const match = String(input).trim().toLowerCase().match(/^([1-9]\d*)(s|m|h|d|w)$/);
  if (!match) throw new RangeError("Use a whole number and s, m, h, d or w, such as 3m or 1w.");
  const seconds = Number(match[1]) * UNITS[match[2]];
  if (!Number.isSafeInteger(seconds) || seconds > 604800) throw new RangeError("Choose an interval between 1 second and 1 week.");
  const unit = seconds === 604800 ? "w" : seconds % 86400 === 0 ? "d" : seconds % 3600 === 0 ? "h" : seconds % 60 === 0 ? "m" : "s";
  return { seconds, milliseconds: seconds * 1e3, label: `${seconds / UNITS[unit]}${unit}` };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/agent-analysis.js
var finite3 = (n) => typeof n === "number" && Number.isFinite(n);
var volumeStudies2 = /* @__PURE__ */ new Set(["vwma", "vwap", "mfi", "obv", "adl", "cmf", "force", "chaikin", "volosc", "eom", "tickvolume"]);
var publicIdentity = ["network", "market", "asset", "baseMint", "quoteMint", "pool", "series", "aggregation", "venue", "historyPolicy", "historySourcePolicy", "instrumentType", "base", "quote"];
var own = (o, keys) => Object.fromEntries(keys.filter((k) => o?.[k] !== void 0).map((k) => [k, o[k]]));
var stamp = (t) => finite3(t) ? new Date(t).toISOString() : null;
function barCopy(b) {
  return { ...own(b, ["time", "open", "high", "low", "close", "volume", "baseVolume", "quoteVolume", "count", "quoteCurrency", "synthetic", "estimated", "estimateMethod", "referenceOnly", "sourceTime", "sourceCandleState", "sourcePriceType", "mixedSource"]), timeUTC: stamp(b.time), ...Array.isArray(b.quoteCurrencies) ? { quoteCurrencies: [...new Set(b.quoteCurrencies.filter((v) => typeof v === "string").map((v) => v.slice(0, 80)))].slice(0, 32) } : {}, sources: (b.sources || []).slice(0, 32).filter((s) => typeof s === "string").map((s) => s.slice(0, 256)), quality: own(b.quality, ["basis", "observedMinutes", "referenceMinutes", "estimatedMinutes", "carriedMinutes"]) };
}
function indicatorCatalog() {
  return INDICATORS.map((d2) => ({ id: d2.id, name: d2.name, short: d2.short, inputs: d2.ohlcv ? "recorded_ohlcv" : "price_or_earlier_study", requiresRecordedVolume: volumeStudies2.has(d2.id), defaults: { ...d2.defaults }, parameters: d2.fields.map(([id, label, minimum, maximum, step = 1]) => ({ id, label, minimum, maximum, step })), customFormula: !!d2.custom, pineSubset: !!d2.pine }));
}
function analyzeMarket(frame, { studies = [], at = null, now = Date.now(), maxAgeMs = 12e4, includeIncomplete = false, includeCandles = false, candleLimit = 100 } = {}) {
  if (!frame || !["executed_history", "asset_history", "exchange_history"].includes(frame.mode) || !frame.identity || !["solana", "arbitrum", "robinhood", "exchange"].includes(frame.identity.network) || !Array.isArray(frame.candles) || frame.candles.length > 2e4) throw Error("Invalid retained market frame.");
  if (!finite3(now) || !finite3(maxAgeMs) || maxAgeMs < 1e3 || maxAgeMs > 864e5 || !Number.isInteger(candleLimit) || candleLimit < 1 || candleLimit > 600 || typeof includeIncomplete !== "boolean" || typeof includeCandles !== "boolean") throw Error("Invalid analysis bounds.");
  if (frame.mint && frame.identity.baseMint !== frame.mint || frame.quoteMint && frame.identity.quoteMint !== frame.quoteMint || frame.pool && frame.identity.pool && frame.identity.pool !== frame.pool) throw Error("Inconsistent source market identity.");
  if (!Array.isArray(studies) || studies.some((s) => !INDICATORS.some((d2) => d2.id === s?.id))) throw Error("Choose native study IDs from indicatorCatalog().");
  const configs = normalizeIndicators(studies), period = parseInterval(frame.interval).milliseconds, input = frame.candles;
  let previous = 0;
  for (const b of input) {
    if (!["time", "open", "high", "low", "close", "volume"].every((k) => finite3(b[k])) || !Number.isSafeInteger(b.time) || b.time <= previous || b.low <= 0 || b.high < Math.max(b.open, b.close) || b.low > Math.min(b.open, b.close) || b.volume < 0 || typeof (b.quoteCurrency || frame.quote) !== "string" || !(b.quoteCurrency || frame.quote)) throw Error("Invalid source candle sequence or quote.");
    previous = b.time;
  }
  if (at !== null && (!finite3(at) || !input.length || at < input[0].time || at > input.at(-1).time)) throw Error("Replay time must be inside retained history.");
  const cutoff = at === null ? now : at, bars = input.filter((b) => b.time <= cutoff && (includeIncomplete || b.time + period <= cutoff)), last = bars.at(-1), prior = bars.at(-2), gaps = [];
  let missing = 0, quoteChanges = 0, trailing = last ? 1 : 0;
  for (let i = 1; i < bars.length; i++) {
    const a = bars[i - 1], b = bars[i], elapsed = b.time - a.time;
    if (elapsed > period) {
      const count = Math.max(0, Math.ceil(elapsed / period) - 1);
      missing += count;
      if (gaps.length < 100) gaps.push({ start: a.time + period, endExclusive: b.time, missingIntervals: count });
    }
    if ((b.quoteCurrency || frame.quote) !== (a.quoteCurrency || frame.quote)) quoteChanges++;
  }
  for (let i = bars.length - 1; i > 0; i--) {
    if (bars[i].synthetic || bars[i - 1].synthetic || bars[i].time - bars[i - 1].time !== period || (bars[i].quoteCurrency || frame.quote) !== (bars[i - 1].quoteCurrency || frame.quote)) break;
    trailing++;
  }
  const age = last ? Math.max(0, cutoff - last.time - period) : null, blockers = [];
  if (!last) blockers.push("no_eligible_candles");
  if (frame.loading) blockers.push("loading_retained_frame");
  if (frame.error) blockers.push("source_error_retained_frame");
  if (frame.requestedInterval && frame.requestedInterval !== frame.interval) blockers.push("requested_interval_not_displayed");
  if (frame.stale || age !== null && age > maxAgeMs) blockers.push("stale_source");
  if (last?.synthetic) blockers.push("carried_close");
  if (last?.estimated) blockers.push("estimated_reference");
  if (last?.sourceCandleState === "provisional") blockers.push("provisional_source");
  const series = bars.map((b) => b.quoteCurrency ? b : { ...b, quoteCurrency: frame.quote }), computed = computeIndicators(series, configs, period, { strict: true }), index = series.length - 1, segment = sourceSegment(series, period, frame.quote);
  trailing = segment.bars.length;
  for (const issue of last ? barIssues(last, last.quoteCurrency || frame.quote) : []) if (!blockers.includes(issue)) blockers.push(issue);
  const outputs = computed.map((s) => {
    const current = Object.fromEntries(Object.entries(s.lines).map(([key, values]) => [key, values[index] ?? null])), previous2 = Object.fromEntries(Object.entries(s.lines).map(([key, values]) => [key, values[index - 1] ?? null]));
    const needsVolume = volumeStudies2.has(s.id), volumeKnown = !!last && !last.synthetic && !last.referenceOnly && !last.estimated;
    const unavailable = s.error ? "calculation_error" : needsVolume && !volumeKnown ? "recorded_volume_unavailable" : Object.values(current).every((v) => v === null) ? s.id === "vwap" ? "incomplete_session_or_volume_history" : "warmup_or_discontinuity" : null;
    return { instance: s.uid, family: s.id, input: s.source, parameters: own(s, Object.keys(s).filter((k) => !["definition", "lines", "error", "uid", "id", "source", "color"].includes(k))), time: last?.time ?? null, values: unavailable ? Object.fromEntries(Object.keys(current).map((k) => [k, null])) : current, previousValues: previous2, valueAvailable: !unavailable, unavailableReason: unavailable, eligibleForAnalysis: !unavailable && !blockers.length, basis: "untransformed_source_candles", calculationVersion: CALCULATION_VERSION, dependencyQuality: { policy: ANALYSIS_POLICY, segmentStart: segment.bars[0]?.time ?? null, continuousEligibleBars: segment.bars.length, excludedInputs: segment.exclusions } };
  });
  const changes = frame.reconciliation || {}, report = { schema: "ticking.agent.snapshot.v1", timing: { generatedAt: now, generatedAtUTC: stamp(now), mode: at === null ? "live" : "replay", replayAt: at, asOf: last?.time ?? null, asOfUTC: stamp(last?.time), interval: frame.interval, intervalMs: period, ageAfterIntervalMs: age, maxAgeMs, includeIncomplete, closedByClock: !!last && last.time + period <= cutoff, finality: "not_proven_by_interval_clock" }, selection: { market: frame.market || null, mint: frame.mint || null, pool: frame.pool || null, aggregation: frame.aggregation || null, quote: last?.quoteCurrency || frame.quote || null, identity: own(frame.identity, publicIdentity) }, quality: { eligibleForAnalysis: !blockers.length, blockers, referenceSeries: frame.identity.series === "reference", upstreamComplete: false, reportedComplete: frame.coverage?.complete === true, scope: "returned_window", inputBars: input.length, analyzedBars: bars.length, trailingContinuousBars: trailing, missingIntervals: missing, gaps, quoteChanges, mixedSourceBars: bars.filter((b) => b.mixedSource).length, carriedBars: bars.filter((b) => b.synthetic).length, estimatedBars: bars.filter((b) => b.estimated).length, referenceOnlyBars: bars.filter((b) => b.referenceOnly).length, sourceHistoryPolicy: frame.coverage?.historyPolicy || frame.identity.historyPolicy || null, volumeDefinition: frame.coverage?.volumeDefinition || null, gapDisclosure: typeof frame.gapDisclosure === "string" ? frame.gapDisclosure : null, executionQuote: false }, candle: last ? barCopy(last) : null, previousCandle: prior ? barCopy(prior) : null, studies: outputs, corrections: { scope: "delivered_window", upstreamComplete: false, reset: !!changes.reset, correctedTimes: (changes.corrections || []).slice(-600).map((c) => c.time).filter(finite3), insertedTimes: (changes.inserted || []).slice(-600).filter(finite3), removedTimes: (changes.removed || []).slice(-600).filter(finite3) } };
  if (includeCandles) report.candles = bars.slice(-candleLimit).map(barCopy);
  return report;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/market-structure.js
var STRUCTURE_POLICY = Object.freeze({ version: "market-structure-v1", maxBars: 600, atr: 14, internal: { left: 3, right: 3, reaction: 0.75 }, swing: { left: 8, right: 8, reaction: 1.5 }, breakBuffer: 0.05, staleBars: 48 });
var number2 = Number.isFinite;
var level = (b, k, basis) => basis === "body" ? k === "low" ? Math.min(b.open, b.close) : Math.max(b.open, b.close) : b[k];
function causalRanges(bars) {
  let sum2 = 0, atr = null;
  return bars.map((b, i) => {
    const tr = Math.max(b.high - b.low, i ? Math.abs(b.high - bars[i - 1].close) : 0, i ? Math.abs(b.low - bars[i - 1].close) : 0);
    sum2 += tr;
    if (i < 14) atr = sum2 / (i + 1);
    else atr += (tr - atr) / 14;
    return atr;
  });
}
function reactionPivots(bars, { period, basis = "wick", left = 3, right = 3, reaction = 0.75 } = {}) {
  const atr = causalRanges(bars), out = [];
  for (const kind of ["low", "high"]) for (let first = left; first + right < bars.length; first++) {
    const p4 = level(bars[first], kind, basis), sign = kind === "low" ? 1 : -1;
    if (level(bars[first - 1], kind, basis) === p4) continue;
    let end = first;
    while (end + 1 < bars.length && level(bars[end + 1], kind, basis) === p4) end++;
    if (end + right >= bars.length) continue;
    const sides = [bars.slice(first - left, first), bars.slice(end + 1, end + right + 1)];
    if (!sides.flat().every((b) => sign * (level(b, kind, basis) - p4) > 0)) continue;
    const magnitude = Math.min(...sides.map((side) => Math.max(...side.map((b) => sign * (level(b, kind === "low" ? "high" : "low", basis) - p4))))), eps = Number.EPSILON * Math.max(1, Math.abs(p4)) * 32;
    if (magnitude < Math.max(eps, atr[end + right] * reaction)) continue;
    out.push({ kind, index: end, time: bars[end].time, price: p4, clusterStart: bars[first].time, clusterBars: end - first + 1, confirmedAt: bars[end + right].time + period, reaction: magnitude, confirmationRange: atr[end + right] });
  }
  return out.sort((a, b) => a.time - b.time || a.kind.localeCompare(b.kind));
}
function layer(bars, { period, basis, left, right, reaction }) {
  const atr = causalRanges(bars), all = reactionPivots(bars, { period, basis, left, right, reaction }), ambiguous = /* @__PURE__ */ new Set();
  for (let i = 1; i < all.length; i++) if (all[i].time === all[i - 1].time) ambiguous.add(all[i].time);
  const pivots = all.filter((p4) => !ambiguous.has(p4.time)), available = [...pivots].sort((a, b) => a.confirmedAt - b.confirmedAt || a.time - b.time), highs = [], lows = [], events = [], broken = /* @__PURE__ */ new Set(), swept = /* @__PURE__ */ new Set();
  let next = 0, bias = 0, transition = 0, protectedPivot = null, pendingAt = null, pendingOrigin = null, establishedAt = null, lastProgress = -1;
  const side = (d2) => d2 > 0 ? "up" : "down", relation = (ps, positive, negative) => ps.length < 2 ? null : ps.at(-1).price > ps.at(-2).price ? positive : ps.at(-1).price < ps.at(-2).price ? negative : "equal";
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], closeAt = b.time + period, previous = bars[i - 1]?.close;
    while (next < available.length && available[next].confirmedAt <= closeAt) {
      const p4 = available[next++];
      (p4.kind === "high" ? highs : lows).push(p4);
    }
    const high = highs.at(-1), low = lows.at(-1), hs = relation(highs, "higher_high", "lower_high"), ls = relation(lows, "higher_low", "lower_low"), eps = Number.EPSILON * Math.max(1, Math.abs(b.close)) * 32, buffer = Math.max(eps, (i ? atr[i - 1] : atr[i]) * 0.05);
    if (!bias && !transition && high && low) {
      if (hs === "higher_high" && ls === "higher_low" && b.close >= low.price - buffer) {
        bias = 1;
        protectedPivot = low;
        establishedAt = closeAt;
        lastProgress = i;
      } else if (hs === "lower_high" && ls === "lower_low" && b.close <= high.price + buffer) {
        bias = -1;
        protectedPivot = high;
        establishedAt = closeAt;
        lastProgress = i;
      }
    }
    const event = (type, d3, p4) => {
      const origin = (d3 > 0 ? lows : highs).findLast((v) => v.time < b.time) || null;
      events.push({ type, direction: side(d3), time: b.time, confirmedAt: closeAt, level: p4.price, pivotTime: p4.time, pivotConfirmedAt: p4.confirmedAt, origin: origin ? { time: origin.time, price: origin.price, kind: origin.kind, confirmedAt: origin.confirmedAt } : null, close: b.close, buffer });
      broken.add(p4.kind + ":" + p4.time);
      lastProgress = i;
      return origin;
    };
    if (bias && protectedPivot && !transition && (bias > 0 ? b.close < protectedPivot.price - buffer : b.close > protectedPivot.price + buffer)) {
      pendingOrigin = event("choch", -bias, protectedPivot);
      transition = -bias;
      bias = 0;
      pendingAt = closeAt;
      continue;
    }
    if (transition && pendingOrigin && (transition > 0 ? b.close < pendingOrigin.price - buffer : b.close > pendingOrigin.price + buffer)) {
      event("failed_choch", -transition, pendingOrigin);
      bias = -transition;
      transition = 0;
      pendingAt = null;
      pendingOrigin = null;
      continue;
    }
    const d2 = transition || bias;
    if (!d2 || !high || !low || previous === void 0) continue;
    const target = d2 > 0 ? high : low, inside = d2 > 0 ? previous <= target.price + buffer : previous >= target.price - buffer, crossed = d2 > 0 ? b.close > target.price + buffer : b.close < target.price - buffer, key = target.kind + ":" + target.time;
    const newTarget = !transition || target.time >= pendingAt - period;
    if (!broken.has(key) && inside && crossed && target.confirmedAt <= b.time && newTarget) {
      const origin = d2 > 0 ? low : high, validOrigin = !transition || origin.time >= pendingAt;
      if (validOrigin) {
        const wasTransition = !!transition;
        event(wasTransition ? "reversal_bos" : "bos", d2, target);
        bias = d2;
        transition = 0;
        pendingAt = null;
        pendingOrigin = null;
        protectedPivot = origin;
        establishedAt = wasTransition ? closeAt : establishedAt;
      }
    } else if (!crossed && !swept.has(key) && (d2 > 0 ? b.high > target.price + buffer : b.low < target.price - buffer) && target.confirmedAt <= b.time) {
      events.push({ type: "wick_sweep", direction: side(d2), time: b.time, confirmedAt: closeAt, level: target.price, pivotTime: target.time, pivotConfirmedAt: target.confirmedAt, origin: null, close: b.close, buffer });
      swept.add(key);
    }
  }
  const highStructure = relation(highs, "higher_high", "lower_high"), lowStructure = relation(lows, "higher_low", "lower_low"), stale = lastProgress >= 0 && bars.length - 1 - lastProgress > STRUCTURE_POLICY.staleBars;
  const state = transition ? "transition" : !bias ? "unconfirmed" : stale ? "stale" : "active";
  return { left, right, minimumReaction: reaction, pivots, ambiguousPivots: ambiguous.size, latestHigh: highs.at(-1) || null, latestLow: lows.at(-1) || null, highStructure, lowStructure, regime: bias > 0 && !stale ? "rising" : bias < 0 && !stale ? "falling" : "undetermined", direction: transition || bias ? side(transition || bias) : null, state, establishedAt, protectedPivot, transitionDirection: transition ? side(transition) : null, transitionAt: pendingAt, events };
}
function marketStructure(bars, { period, basis = "wick", left = 3, right = 3 } = {}) {
  if (!Array.isArray(bars) || bars.length > 600 || !number2(period) || period <= 0 || !["wick", "body"].includes(basis) || ![left, right].every((v) => Number.isInteger(v) && v >= 1 && v <= 20)) throw Error("Invalid bounded market-structure input.");
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    if (!["time", "open", "high", "low", "close"].every((k) => number2(b[k])) || !Number.isSafeInteger(b.time) || b.low <= 0 || b.high < Math.max(b.open, b.close) || b.low > Math.min(b.open, b.close) || i && b.time - bars[i - 1].time !== period) throw Error("Market structure requires consecutive valid source candles.");
  }
  const internal = layer(bars, { period, basis, left, right, reaction: 0.75 }), swing = layer(bars, { period, basis, left: Math.max(8, left * 2), right: Math.max(8, right * 2), reaction: 1.5 });
  const primary = swing.state === "active" || swing.state === "transition" ? "swing" : "internal", selected = primary === "swing" ? swing : internal;
  return { method: STRUCTURE_POLICY.version, basis, primary, regime: selected.regime, state: selected.state, direction: selected.state === "unconfirmed" ? null : selected.direction, transitionDirection: selected.transitionDirection, transitionAt: selected.transitionAt, protectedPivot: selected.protectedPivot, establishedAt: selected.establishedAt, latestHigh: selected.latestHigh, latestLow: selected.latestLow, highStructure: selected.highStructure, lowStructure: selected.lowStructure, pivots: internal.pivots, internal, swing, scope: "completed source bars; causal confirmation; CHOCH is a warning, not a confirmed reversal", cutoff: bars.at(-1) ? bars.at(-1).time + period : null, bars: bars.length };
}
function compactStructure(s) {
  const trim = (l) => ({ ...l, pivots: void 0, events: l.events.slice(-12) });
  return { ...s, pivots: void 0, internal: trim(s.internal), swing: trim(s.swing) };
}
function structureContext(s) {
  return { method: s.method, contextBars: s.bars, primary: s.primary, state: s.state, direction: s.direction, regime: s.regime, transitionDirection: s.transitionDirection, transitionAt: s.transitionAt, protectedPivot: s.protectedPivot ? { time: s.protectedPivot.time, price: s.protectedPivot.price, kind: s.protectedPivot.kind } : null, lastEvent: s[s.primary].events.filter((e) => e.type !== "wick_sweep").at(-1) || null, internalState: s.internal.state, internalDirection: s.internal.direction };
}
function describeStructure(s) {
  const e = s.lastEvent;
  return `Market structure \xB7 ${s.method}: ${s.primary} ${s.state}${s.direction ? " " + s.direction : ""}. Internal: ${s.internalState}${s.internalDirection ? " " + s.internalDirection : ""}.${s.protectedPivot ? " Protected " + s.protectedPivot.kind + " " + Number(s.protectedPivot.price.toPrecision(8)) + "." : ""}${e ? " Last " + e.type.toUpperCase().replaceAll("_", " ") + " " + e.direction + " at " + new Date(e.confirmedAt).toISOString().slice(0, 16) + " UTC." : ""}${s.state === "transition" ? " The protected trend is transitioning; an opposite follow-through BOS is required before confirming reversal. This is context, not a standalone entry signal." : ""}`;
}
function structureConforms(a, b) {
  const keys = ["method", "basis", "primary", "regime", "state", "direction", "transitionDirection", "transitionAt", "protectedPivot", "establishedAt", "latestHigh", "latestLow", "highStructure", "lowStructure", "internal", "swing", "scope", "cutoff", "bars"];
  const same = (x, y) => {
    if (x === y) return true;
    if (typeof x === "number" && typeof y === "number") return Number.isFinite(x) && Number.isFinite(y) && Math.abs(x - y) <= 1e-10 * Math.max(1e-12, Math.abs(x), Math.abs(y));
    if (!x || !y || typeof x !== "object" || typeof y !== "object" || Array.isArray(x) !== Array.isArray(y)) return false;
    const ax = Object.keys(x).filter((k) => x[k] !== void 0), by = Object.keys(y).filter((k) => y[k] !== void 0);
    return ax.length === by.length && ax.every((k) => Object.hasOwn(y, k) && same(x[k], y[k]));
  };
  return keys.every((k) => same(a?.[k], b?.[k]));
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/chart-trend-anchors.js
var SWING_POLICY = Object.freeze({ version: "swing-v3", left: 3, right: 3, maxBars: 180, minBars: 15, minSeparation: 6, rangePeriod: 14, minimumMove: 0.25, minimumReaction: 0.75, wickTolerance: 0.05, touchTolerance: 0.15 });
var fail = (message) => {
  throw Error(message + " No drawing was added.");
};
var tx = (p4, scale2) => scale2 === "log" ? Math.log(p4) : p4;
function windowData(snapshot, visible, scale2, basis, maxBars = SWING_POLICY.maxBars) {
  const policy = SWING_POLICY, period = snapshot.timing.intervalMs;
  if (!["linear", "log", void 0].includes(scale2)) fail("Use a linear or logarithmic price scale.");
  if (!["wick", "body"].includes(basis)) fail("Choose wick or body anchors.");
  const last = Math.min(visible?.at(-1)?.time ?? Infinity, snapshot.candle?.time ?? 0);
  let bars = snapshot.candles.filter((b) => b.time <= last).slice(-600), start = 0;
  const quote = (b) => b.quoteCurrency || snapshot.selection.quote;
  const identity = snapshot.selection.identity, modeled = (b) => b.estimated && b.referenceOnly && b.estimateMethod === "median_venue_ohlc_v1" && identity?.network === "exchange" && identity.venue === "ticking" && identity.series === "reference" && identity.historyPolicy === "median_venue_ohlc_v1" && identity.aggregation === "multi_venue_asset_reference_v1";
  const invalid = (b) => b.synthetic || b.estimated && !modeled(b) || b.sourceCandleState === "provisional" || !quote(b) || b.quoteCurrencies?.length > 1 || b.quoteCurrencies?.length === 1 && b.quoteCurrencies[0] !== quote(b) || b.mixedSource && b.quoteCurrencies?.length !== 1;
  for (let i = 0; i < bars.length; i++) if (invalid(bars[i])) start = i + 1;
  else if (i && (bars[i].time - bars[i - 1].time !== period || quote(bars[i]) !== quote(bars[i - 1]))) start = i;
  bars = bars.slice(start);
  const contextBars = bars;
  if (bars.length < policy.minBars) fail("The available history has fewer than 15 uninterrupted completed candles in one quote. Missing, unsupported estimated, provisional or ambiguous-quote candles end the usable segment. This is a source-history limit; changing zoom is not required.");
  const structure = marketStructure(contextBars, { period, basis }), offset = Math.max(0, bars.length - maxBars);
  bars = bars.slice(offset);
  const range = bars.slice(-policy.rangePeriod).map((b, i, all) => {
    const previous = i ? all[i - 1] : bars[bars.length - all.length - 1];
    return Math.max(tx(b.high, scale2) - tx(b.low, scale2), previous ? Math.abs(tx(b.high, scale2) - tx(previous.close, scale2)) : 0, previous ? Math.abs(tx(b.low, scale2) - tx(previous.close, scale2)) : 0);
  }), atr = range.reduce((s, v) => s + v, 0) / range.length;
  const epsilon = Math.max(Number.EPSILON * Math.max(1, Math.abs(tx(bars.at(-1).close, scale2))) * 32, Number.MIN_VALUE), wick = Math.max(epsilon, atr * policy.wickTolerance), touch = Math.max(epsilon, atr * policy.touchTolerance), pivots = [];
  pivots.push(...structure.internal.pivots.filter((p4) => p4.index >= offset).map((p4) => ({ ...p4, index: p4.index - offset })));
  return { bars, pivots, structure, period, atr, epsilon, wick, touch, scale: scale2 || "linear", basis, quote: quote(bars.at(-1)), modeledReferenceBars: bars.filter(modeled).length, cutoff: bars.at(-1).time + period, referenceBars: snapshot.quality?.referenceSeries ? bars.length : bars.filter((b) => b.referenceOnly || b.mixedSource || b.quality?.referenceMinutes > 0 || (b.sources || []).some((s) => /reference|archive/i.test(s))).length };
}
var RECENT_CHANNEL_POLICY = Object.freeze({ version: "recent-channel-v1", maxBars: 600, minLegBars: 5, minFitR2: 0.35, minMoveRanges: 0.75, maxTurnAge: 48 });

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/chart-fib-anchors.js
var FIB_WAVE_POLICY = Object.freeze({ version: "fib-wave-v2", maxBars: 600, minSeparation: 6, minimumMove: 2 });
var extreme = (b, kind, basis) => basis === "body" ? kind === "low" ? Math.min(b.open, b.close) : Math.max(b.open, b.close) : b[kind];
var fail2 = (text) => {
  throw Error(text + " No Fibonacci drawing was added.");
};
function fibonacciWaveAnchors(snapshot, visible, direction = "auto", basis = "wick", { preferRecent = false } = {}) {
  if (!["auto", "up", "down"].includes(direction)) fail2("Choose an ascending, descending or automatic Fibonacci wave.");
  const w = windowData(snapshot, visible, "linear", basis, FIB_WAVE_POLICY.maxBars), { bars, epsilon, atr } = w, minimum = FIB_WAVE_POLICY.minimumMove, context = structureContext(w.structure), preferred = direction === "auto" && !preferRecent && ["active", "transition"].includes(context.state) ? context.direction : direction, events = [...w.structure.internal.events.map((e) => ({ ...e, layer: "internal" })), ...w.structure.swing.events.map((e) => ({ ...e, layer: "swing" }))].filter((e) => ["bos", "choch", "reversal_bos"].includes(e.type)), ambiguous = /* @__PURE__ */ new Set();
  for (let i = 1; i < w.pivots.length; i++) if (w.pivots[i].time === w.pivots[i - 1].time && w.pivots[i].kind !== w.pivots[i - 1].kind) ambiguous.add(w.pivots[i].time);
  const turns = [];
  for (const p4 of w.pivots) {
    if (ambiguous.has(p4.time)) continue;
    const last = turns.at(-1);
    if (!last) {
      turns.push(p4);
      continue;
    }
    if (p4.kind === last.kind) {
      if (p4.kind === "high" ? p4.price >= last.price : p4.price <= last.price) turns[turns.length - 1] = p4;
      continue;
    }
    const sign = last.kind === "low" ? 1 : -1;
    if (p4.index - last.index < FIB_WAVE_POLICY.minSeparation || sign * (p4.price - last.price) < Math.max(epsilon, p4.confirmationRange * minimum)) continue;
    turns.push(p4);
  }
  const fits = [];
  let originBroken = 0;
  for (let i = 1; i < turns.length; i++) {
    const a = turns[i - 1], b = turns[i], up = a.kind === "low", side = up ? "up" : "down", sign = up ? 1 : -1;
    if (preferred !== "auto" && preferred !== null && preferred !== side || a.time >= b.time || b.index - a.index < FIB_WAVE_POLICY.minSeparation || sign * (b.price - a.price) < Math.max(epsilon, b.confirmationRange * minimum)) continue;
    const segment = bars.slice(a.index, b.index + 1), later = bars.slice(b.index + 1);
    if (segment.some((c) => extreme(c, "low", basis) < Math.min(a.price, b.price) - epsilon || extreme(c, "high", basis) > Math.max(a.price, b.price) + epsilon)) continue;
    if (later.some((c) => sign * (extreme(c, a.kind, basis) - a.price) < -epsilon)) {
      originBroken++;
      continue;
    }
    if (!preferRecent && context.state === "transition" && b.time < context.transitionAt - w.period) continue;
    const structureEvent = events.filter((e) => e.direction === side && e.time >= a.time && e.time <= b.time).sort((x, y) => y.time - x.time || (x.layer === "swing" ? -1 : 1))[0] || null;
    const priorEnd = turns.slice(0, i - 1).findLast((p4) => p4.kind === b.kind), priorStart = turns.slice(0, i - 1).findLast((p4) => p4.kind === a.kind);
    const closeBreak = priorEnd && sign * (b.price - priorEnd.price) > epsilon ? segment.find((c) => sign * (c.close - priorEnd.price) > epsilon) : null;
    const structured = !!priorStart && !!priorEnd && sign * (a.price - priorStart.price) > epsilon && sign * (b.price - priorEnd.price) > epsilon;
    const extension = later.find((c) => sign * (extreme(c, b.kind, basis) - b.price) > epsilon), last = bars.at(-1), ratio = (b.price - last.close) / (b.price - a.price);
    fits.push({ points: [a, b], direction: side, basis, span: b.index - a.index, move: Math.abs(b.price - a.price), moveInRange: atr > epsilon ? Math.abs(b.price - a.price) / atr : null, breaksStructure: !!structureEvent, structureEvent, trendContext: context, priorEnd: structureEvent ? { kind: up ? "high" : "low", price: structureEvent.level, time: structureEvent.pivotTime } : null, breakTime: structureEvent?.time ?? null, structure: structured ? up ? "higher low \u2192 higher high" : "lower high \u2192 lower low" : closeBreak ? "prior swing broken; trend transition" : "swing leg; trend not confirmed", confirmedAt: b.confirmedAt, endpointExtended: !!extension, extensionTime: extension?.time ?? null, pullbackRatio: ratio, state: extension ? "extended_after_confirmation" : ratio >= 1 ? "full_retracement" : ratio <= 0 ? "at_endpoint" : "retracement", windowBars: bars.length, windowStart: bars[0].time, cutoff: w.cutoff, quote: w.quote, referenceBars: w.referenceBars, modeledReferenceBars: w.modeledReferenceBars, ambiguousPivots: w.structure.internal.ambiguousPivots, originBroken, rule: FIB_WAVE_POLICY.version });
  }
  fits.sort((a, b) => preferRecent ? b.points[1].time - a.points[1].time || Number(b.breaksStructure) - Number(a.breaksStructure) || b.points[0].time - a.points[0].time : Number(b.breaksStructure) - Number(a.breaksStructure) || (b.structureEvent?.time ?? 0) - (a.structureEvent?.time ?? 0) || b.points[1].time - a.points[1].time || b.points[0].time - a.points[0].time);
  if (!fits.length) fail2(`No ${direction === "auto" ? "" : direction === "up" ? "ascending " : "descending "}confirmed swing leg passes ${FIB_WAVE_POLICY.version} in the available ${bars.length} uninterrupted completed candles. ${context.state === "transition" ? "A " + context.transitionDirection + " CHOCH is only a warning; wait for a confirmed endpoint of the new impulse. " : ""}Need alternating reaction pivots, at least 6 bars apart and a move of at least 2\xD7 the endpoint confirmation-time Wilder range, with an intact origin. ${originBroken ? originBroken + " origins were crossed after their endpoints. " : ""}Unconfirmed and same-bar ambiguous extremes are not used. More source history or manual anchors are needed; changing zoom is not required.`);
  return { ...fits[0], eligibleCandidates: fits.length, confirmedTurns: turns.length, selectionPolicy: preferRecent ? "nearest_confirmed_leg" : "structure_first" };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/strategy-evidence.js
var EVIDENCE_POLICY = "chart-evidence-v1";
var EVIDENCE_KINDS = ["", "rsi_divergence", "triangle", "rising_wedge", "falling_wedge", "channel", "failed_breakout", "zone_rejection", "trendline_reclaim", "double_reversal", "head_shoulders", "macd_cross", "macd_divergence", "band_reentry", "ema_pullback", "candle_rejection", "triple_reversal", "volatility_squeeze"];
function chartEvidence(bars, period) {
  const cutoff = bars.at(-1)?.time + period || 0, out = [];
  if (bars.length < 50) return { method: EVIDENCE_POLICY, cutoff, observations: out };
  const n = bars.length, z = n - 1, b = bars[z], atr = causalRanges(bars).at(-1), structure = marketStructure(bars, { period }), pivots = structure.internal.pivots, rs = Array(n).fill(0);
  let gain = 0, loss = 0;
  for (let i = 1; i < n; i++) {
    const d2 = bars[i].close - bars[i - 1].close;
    if (i <= 14) {
      gain += Math.max(0, d2) / 14;
      loss += Math.max(0, -d2) / 14;
    } else {
      gain += (Math.max(0, d2) - gain) / 14;
      loss += (Math.max(0, -d2) - loss) / 14;
    }
    if (i >= 14) rs[i] = loss === 0 ? gain === 0 ? 50 : 100 : 100 - 100 / (1 + gain / loss);
  }
  const add2 = (kind, side, status, first, last, level2, stopAnchor, target = 0, extra = {}) => {
    if (out.length < 24) out.push({ kind: EVIDENCE_KINDS[kind], side, status, first: bars[first].time, last: bars[last].time, confirmedAt: cutoff, level: level2, stopAnchor, target, priceA: 0, priceB: 0, oscillatorA: 0, oscillatorB: 0, upper: 0, lower: 0, upperSlope: 0, lowerSlope: 0, touches: 0, ...extra });
  };
  for (const [kind, side] of [["low", 1], ["high", -1]]) {
    const ps = pivots.filter((p5) => p5.kind === kind && p5.index >= 14), q = ps.at(-1), p4 = ps.at(-2);
    if (!p4 || !q || q.index - p4.index < 6 || q.index - p4.index > 80 || z - q.index > 12) continue;
    if (side * (q.price - p4.price) >= -atr * 0.1 || side * (rs[q.index] - rs[p4.index]) < 3) continue;
    if (bars.slice(q.index).some((v) => side > 0 ? v.low < q.price - atr * 0.05 : v.high > q.price + atr * 0.05)) continue;
    const between = bars.slice(p4.index + 1, q.index), level2 = side > 0 ? Math.max(...between.map((v) => v.high)) : Math.min(...between.map((v) => v.low));
    if (side * (b.close - level2) > atr * 0.05) continue;
    add2(1, side, "observed", p4.index, q.index, level2, q.price, 0, { priceA: p4.price, priceB: q.price, oscillatorA: rs[p4.index], oscillatorB: rs[q.index], touches: 2 });
  }
  const recent = pivots.filter((p4) => p4.index >= Math.max(14, n - 120)), highs = recent.filter((p4) => p4.kind === "high").slice(-3), lows = recent.filter((p4) => p4.kind === "low").slice(-3);
  if (highs.length >= 2 && lows.length >= 2) {
    const h1 = highs.at(-2), h2 = highs.at(-1), l1 = lows.at(-2), l2 = lows.at(-1), start = Math.min(h1.index, l1.index), end = Math.max(h2.index, l2.index), hs = (h2.price - h1.price) / (h2.index - h1.index), ls = (l2.price - l1.price) / (l2.index - l1.index), hi = (i) => h1.price + hs * (i - h1.index), lo = (i) => l1.price + ls * (i - l1.index), width = hi(start) - lo(start), current = hi(z) - lo(z), flat = atr * 0.01;
    const contained = Math.max(h1.index, l1.index) < Math.min(h2.index, l2.index) && width > atr && current > atr * 0.5 && end - start >= 12 && z - end <= 20 && bars.slice(start, z).every((v, j) => v.high <= hi(start + j) + atr * 0.25 && v.low >= lo(start + j) - atr * 0.25);
    if (contained) {
      const compression = current < width * 0.8 && hs < ls, kind = compression ? hs > flat && ls > flat ? 3 : hs < -flat && ls < -flat ? 4 : hs <= flat && ls >= -flat ? 2 : 0 : 0, parallel = Math.abs(hs - ls) <= atr * 0.015;
      const shape = kind || (parallel ? 5 : 0), extra = { upper: hi(z), lower: lo(z), upperSlope: hs, lowerSlope: ls, touches: 4, priceA: h1.price, priceB: h2.price };
      if (shape) {
        const up = b.close > hi(z) + atr * 0.05 && bars[z - 1].close <= hi(z - 1) + atr * 0.05, down = b.close < lo(z) - atr * 0.05 && bars[z - 1].close >= lo(z - 1) - atr * 0.05;
        const dir = up ? 1 : down ? -1 : 0;
        if (!dir && b.high <= hi(z) + atr * 0.25 && b.low >= lo(z) - atr * 0.25) {
          const rejectUp = shape === 5 && b.low <= lo(z) + atr * 0.15 && b.close > lo(z) + atr * 0.3 && b.close > b.open;
          const rejectDown = shape === 5 && b.high >= hi(z) - atr * 0.15 && b.close < hi(z) - atr * 0.3 && b.close < b.open;
          if (rejectUp || rejectDown) {
            const side = rejectUp ? 1 : -1;
            add2(5, side, "rejection", start, z, side > 0 ? b.high : b.low, side > 0 ? Math.min(b.low, lo(z)) : Math.max(b.high, hi(z)), side > 0 ? hi(z) : lo(z), extra);
          } else add2(shape, 0, "forming", start, end, hi(z), lo(z), 0, extra);
        } else if (dir) add2(shape, dir, "breakout", start, z, dir > 0 ? Math.max(b.high, hi(z)) : Math.min(b.low, lo(z)), dir > 0 ? lo(z) : hi(z), 0, extra);
      }
    }
  }
  for (const side of [1, -1]) {
    let found = false;
    for (let i = z - 1; i >= Math.max(20, z - 6) && !found; i--) {
      const p4 = pivots.filter((p5) => p5.kind === (side > 0 ? "low" : "high") && p5.confirmedAt <= bars[i].time).at(-1);
      if (!p4) continue;
      const broken = side > 0 ? bars[i].close < p4.price - atr * 0.05 : bars[i].close > p4.price + atr * 0.05, reclaimed = side > 0 ? b.close > p4.price + atr * 0.05 : b.close < p4.price - atr * 0.05;
      if (broken && reclaimed) {
        const segment = bars.slice(i), stop = side > 0 ? Math.min(...segment.map((v) => v.low)) : Math.max(...segment.map((v) => v.high));
        add2(6, side, "reclaimed", p4.index, z, side > 0 ? b.high : b.low, stop, 0, { priceA: p4.price, priceB: bars[i].close, touches: 1 });
        found = true;
      }
    }
  }
  for (const side of [1, -1]) {
    let found = false;
    for (let i = z - 1; i >= Math.max(20, z - 6) && !found; i--) {
      const ps = pivots.filter((p5) => p5.kind === (side > 0 ? "low" : "high") && p5.confirmedAt <= bars[i].time), q = ps.at(-1), p4 = ps.at(-2);
      if (!p4 || !q || q.index - p4.index < 6 || side * (q.price - p4.price) <= atr * 0.1) continue;
      const slope2 = (q.price - p4.price) / (q.index - p4.index), line = (j) => p4.price + slope2 * (j - p4.index), intact = bars.slice(p4.index, i).every((v, j) => side * (v.close - line(p4.index + j)) >= -atr * 0.05);
      if (intact && side * (bars[i].close - line(i)) < -atr * 0.05 && side * (b.close - line(z)) > atr * 0.05) {
        const seg = bars.slice(i), stop = side > 0 ? Math.min(...seg.map((v) => v.low)) : Math.max(...seg.map((v) => v.high));
        add2(8, side, "reclaimed", p4.index, z, side > 0 ? b.high : b.low, stop, 0, { priceA: line(z), priceB: bars[i].close, upperSlope: slope2, touches: 2 });
        found = true;
      }
    }
  }
  for (const side of [1, -1]) {
    const ps = pivots.filter((p5) => p5.kind === (side > 0 ? "low" : "high") && p5.index >= Math.max(14, n - 120) && p5.confirmedAt <= b.time), p4 = ps.at(-1);
    if (!p4) continue;
    const tests = ps.filter((q) => Math.abs(q.price - p4.price) <= atr * 0.35), distinct = tests.filter((p5, i) => !i || p5.index - tests[i - 1].index >= 6);
    if (distinct.length < 3) continue;
    const low = Math.min(...distinct.map((p5) => p5.price)), high = Math.max(...distinct.map((p5) => p5.price));
    const intact = bars.slice(distinct[0].index, z).every((v) => side > 0 ? v.close >= low - atr * 0.1 : v.close <= high + atr * 0.1), rejection = side > 0 ? b.low <= high + atr * 0.15 && b.low >= low - atr * 0.35 && b.close > high + atr * 0.15 && b.close > b.open : b.high >= low - atr * 0.15 && b.high <= high + atr * 0.35 && b.close < low - atr * 0.15 && b.close < b.open;
    if (intact && rejection) add2(7, side, "rejection", distinct[0].index, z, side > 0 ? b.high : b.low, side > 0 ? Math.min(low, b.low) : Math.max(high, b.high), 0, { priceA: low, priceB: high, touches: distinct.length });
  }
  for (const size of [3, 5]) {
    const ps = pivots.slice(-size);
    if (ps.length !== size || ps.some((p4, i) => i && p4.kind === ps[i - 1].kind)) continue;
    const first = ps[0], q = ps.at(-1), side = first.kind === "low" ? 1 : -1;
    if (q.kind !== first.kind || q.index - first.index < 12 || z - q.index > 30 || q.confirmedAt > b.time) continue;
    const before = bars[Math.max(0, first.index - 20)].close;
    if (side * (first.price - before) > -atr) continue;
    const turns = ps.filter((p4) => p4.kind === first.kind), opposite = ps.filter((p4) => p4.kind !== first.kind), near = Math.abs(q.price - first.price) <= atr * 0.75;
    let kind = 0, neck = 0, height = 0;
    if (size === 3 && near && q.index - first.index >= 12) {
      neck = opposite[0].price;
      height = side > 0 ? neck - Math.min(first.price, q.price) : Math.max(first.price, q.price) - neck;
      if (height >= atr * 1.5) kind = 9;
    }
    if (size === 5 && near) {
      const head = turns[1], isHead = side * (head.price - Math.min(first.price, q.price)) < -atr * 0.75 && side * (head.price - Math.max(first.price, q.price)) < -atr * 0.75;
      if (isHead) {
        kind = 10;
        const slope2 = (opposite[1].price - opposite[0].price) / (opposite[1].index - opposite[0].index);
        neck = opposite[0].price + slope2 * (z - opposite[0].index);
        height = Math.abs(head.price - (opposite[0].price + slope2 * (head.index - opposite[0].index)));
      } else if (Math.max(...turns.map((p4) => p4.price)) - Math.min(...turns.map((p4) => p4.price)) <= atr * 0.75) {
        kind = 16;
        neck = side > 0 ? Math.max(...opposite.map((p4) => p4.price)) : Math.min(...opposite.map((p4) => p4.price));
        height = Math.abs(neck - first.price);
      }
    }
    if (!kind || height < atr * 1.5 || side * (b.close - neck) <= atr * 0.05 || kind !== 10 && side * (bars[z - 1].close - neck) > atr * 0.05) continue;
    if (kind === 10) {
      const slope2 = (opposite[1].price - opposite[0].price) / (opposite[1].index - opposite[0].index), old2 = opposite[0].price + slope2 * (z - 1 - opposite[0].index);
      if (side * (bars[z - 1].close - old2) > atr * 0.05) continue;
    }
    const target = neck + side * height;
    if (target <= 0) continue;
    add2(kind, side, "breakout", first.index, z, side > 0 ? b.high : b.low, q.price, target, { priceA: first.price, priceB: q.price, upper: neck, lower: height, touches: turns.length });
  }
  const ema2 = (values, length2) => {
    let sum2 = 0, count = 0, value = null;
    return values.map((x) => {
      if (!Number.isFinite(x)) {
        sum2 = count = 0;
        value = null;
        return null;
      }
      if (value === null) {
        sum2 += x;
        if (++count === length2) value = sum2 / length2;
      } else value += (x - value) * 2 / (length2 + 1);
      return value;
    });
  };
  const close = bars.map((b2) => b2.close), e12 = ema2(close, 12), e26 = ema2(close, 26), macd = close.map((_, i) => e12[i] === null || e26[i] === null ? null : e12[i] - e26[i]), signal = ema2(macd, 9), e20 = ema2(close, 20), e50 = ema2(close, 50), cross = (x, y, i) => x[i] > y[i] && x[i - 1] <= y[i - 1] ? 1 : x[i] < y[i] && x[i - 1] >= y[i - 1] ? -1 : 0;
  const md = cross(macd, signal, z);
  if (md) add2(11, md, "observed", z - 1, z, md > 0 ? b.high : b.low, md > 0 ? Math.min(b.low, bars[z - 1].low) : Math.max(b.high, bars[z - 1].high), 0, { oscillatorA: macd[z], oscillatorB: signal[z], priceA: bars[z - 1].close, priceB: b.close, touches: 1 });
  for (const side of [1, -1]) {
    const ps = pivots.filter((p5) => p5.kind === (side > 0 ? "low" : "high") && p5.index >= 34), p4 = ps.at(-2), q = ps.at(-1);
    if (p4 && q && q.index - p4.index >= 6 && q.index - p4.index <= 80 && z - q.index <= 12 && side * (q.price - p4.price) < -atr * 0.1 && side * (macd[q.index] - macd[p4.index]) > atr * 0.05 && !bars.slice(q.index).some((v) => side > 0 ? v.low < q.price - atr * 0.05 : v.high > q.price + atr * 0.05)) add2(12, side, "observed", p4.index, q.index, side > 0 ? b.high : b.low, q.price, 0, { priceA: p4.price, priceB: q.price, oscillatorA: macd[p4.index], oscillatorB: macd[q.index], touches: 2 });
  }
  const band = (i) => {
    const v = close.slice(i - 19, i + 1), mid = v.reduce((s, x) => s + x, 0) / 20, dev = Math.sqrt(v.reduce((s, x) => s + (x - mid) ** 2, 0) / 20);
    return { mid, upper: mid + 2 * dev, lower: mid - 2 * dev, width: 4 * dev };
  }, bb = band(z), old = band(z - 1), struct = structure[structure.primary];
  for (const side of [1, -1]) {
    const reentry = side > 0 ? bars[z - 1].close < old.lower && b.close > bb.lower && b.close < bb.mid : bars[z - 1].close > old.upper && b.close < bb.upper && b.close > bb.mid;
    if (reentry && Math.abs(e20[z] - e50[z]) < atr * 0.5 && structure.state !== "transition") add2(13, side, "reclaimed", z - 1, z, side > 0 ? b.high : b.low, side > 0 ? Math.min(b.low, bars[z - 1].low) : Math.max(b.high, bars[z - 1].high), bb.mid, { priceA: side > 0 ? bb.lower : bb.upper, priceB: bb.mid, touches: 1 });
    const protect = struct.protectedPivot, aligned = struct.state === "active" && struct.regime === (side > 0 ? "rising" : "falling") && side * (e20[z] - e50[z]) > atr * 0.1;
    if (aligned && protect && side * (b.close - protect.price) > 0 && side * (b.close - e20[z]) > atr * 0.1 && (side > 0 ? b.low <= e20[z] + atr * 0.15 && b.close > b.open : b.high >= e20[z] - atr * 0.15 && b.close < b.open)) add2(14, side, "rejection", protect.index, z, side > 0 ? b.high : b.low, protect.price, 0, { priceA: e20[z], priceB: protect.price, touches: 1 });
    const body = Math.abs(b.close - b.open), upwick = b.high - Math.max(b.open, b.close), downwick = Math.min(b.open, b.close) - b.low, previous = bars[z - 1], hammer = body > atr * 0.1 && (side > 0 ? downwick >= 2 * body && upwick <= body * 0.5 : upwick >= 2 * body && downwick <= body * 0.5), engulf = side * (previous.close - previous.open) < 0 && side * (b.close - b.open) > 0 && (side > 0 ? b.open <= previous.close && b.close >= previous.open : b.open >= previous.close && b.close <= previous.open), p4 = pivots.filter((p5) => p5.kind === (side > 0 ? "low" : "high") && p5.confirmedAt <= b.time).at(-1);
    if (p4 && side * (b.close - b.open) > 0 && (hammer || engulf) && Math.abs((side > 0 ? b.low : b.high) - p4.price) <= atr * 0.5 && side * (b.close - p4.price) > atr * 0.1) add2(15, side, "rejection", p4.index, z, side > 0 ? b.high : b.low, side > 0 ? Math.min(b.low, p4.price) : Math.max(b.high, p4.price), 0, { priceA: p4.price, priceB: b.close, touches: 1 });
  }
  const widths = Array.from({ length: Math.min(60, n - 20) }, (_, i) => band(z - 1 - i).width).sort((a, b2) => a - b2), median = widths[Math.floor(widths.length / 2)];
  if (bb.width > 0 && bb.width < median * 0.75) add2(17, 0, "forming", Math.max(0, z - 59), z, b.close, 0, 0, { upper: bb.upper, lower: bb.lower, priceA: bb.width, priceB: median, touches: 0 });
  return { method: EVIDENCE_POLICY, cutoff, observations: out };
}
function evidenceConforms(a, b) {
  const same = (x, y) => typeof x === "number" && typeof y === "number" ? Math.abs(x - y) <= 1e-10 * Math.max(1e-12, Math.abs(x), Math.abs(y)) : x === y;
  return a?.method === b?.method && a.cutoff === b.cutoff && a.observations.length === b.observations.length && a.observations.every((x, i) => Object.keys(x).every((k) => same(x[k], b.observations[i][k])));
}
var evidenceTitle = (e) => ({ rsi_divergence: e.side > 0 ? "Bullish RSI divergence" : "Bearish RSI divergence", triangle: "Triangle compression", rising_wedge: "Rising wedge compression", falling_wedge: "Falling wedge compression", channel: e.status === "rejection" ? "Channel boundary rejection" : e.status === "breakout" ? "Channel breakout" : "Parallel channel", failed_breakout: "False close breakout and reclaim", zone_rejection: "Repeated price-zone rejection", trendline_reclaim: "Trendline false break and reclaim", double_reversal: e.side > 0 ? "Double bottom neckline break" : "Double top neckline break", head_shoulders: e.side > 0 ? "Inverse head and shoulders" : "Head and shoulders", macd_cross: e.side > 0 ? "MACD bullish signal cross" : "MACD bearish signal cross", macd_divergence: e.side > 0 ? "Bullish MACD divergence" : "Bearish MACD divergence", band_reentry: "Bollinger band re-entry", ema_pullback: "Protected-trend EMA pullback", candle_rejection: "Support / resistance candle rejection", triple_reversal: e.side > 0 ? "Triple bottom neckline break" : "Triple top neckline break", volatility_squeeze: "Bollinger volatility contraction" })[e.kind] || "Price evidence";

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analyst-evidence-map.js
var DECISION_POLICY = "evidence-context-v4";
var finite4 = Number.isFinite;
var p = (n) => Number(n.toPrecision(8));
var momentum = /* @__PURE__ */ new Set(["rsi_divergence", "macd_cross", "macd_divergence"]);
function deriveSwingRetracement(snapshot, bars, period) {
  try {
    const r = fibonacciWaveAnchors({ ...snapshot, candles: bars, candle: bars.at(-1) }, null), [start, end] = r.points;
    if (r.state !== "retracement" || bars.at(-1).time - end.time > 120 * period) return null;
    const sign = r.direction === "up" ? 1 : -1, span = Math.abs(end.price - start.price), levels = [0.382, 0.5, 0.618, 0.786].map((ratio) => ({ ratio, price: end.price - sign * span * ratio })), nearest = levels.toSorted((a, b) => Math.abs(bars.at(-1).close - a.price) - Math.abs(bars.at(-1).close - b.price))[0];
    const range = snapshot.analysisATR;
    return { method: r.rule, basis: "same confirmed wave selector as chart Fibonacci; wick anchors", side: sign > 0 ? "long" : "short", start: { time: start.time, price: start.price, confirmedAt: start.confirmedAt }, end: { time: end.time, price: end.price, confirmedAt: end.confirmedAt }, structure: r.structure, levels, nearRatio: finite4(range) && Math.abs(bars.at(-1).close - nearest.price) <= range * 0.2 ? nearest.ratio : null, toleranceATR: 0.2 };
  } catch {
    return null;
  }
}
function swingRetracement(f) {
  return f?.eligible ? f.retracement || null : null;
}
function scenarioSupport(f, s, contextDirection) {
  const sign = s.side === "long" ? 1 : -1, obs = f.chartEvidence?.observations || [], kind = s.patternKind;
  const priceTier = momentum.has(kind) ? 1 : ["triangle", "rising_wedge", "falling_wedge"].includes(kind) && s.patternRank < 4 ? 1 : ["head_shoulders", "double_reversal", "triple_reversal", "channel"].includes(kind) ? 4 : 3;
  const momentumAligned = obs.some((e) => e.side === sign && momentum.has(e.kind)), momentumOpposed = obs.some((e) => e.side === -sign && momentum.has(e.kind)), fib = swingRetracement(f), location = !!(fib && fib.side === s.side && fib.nearRatio !== null);
  const context = contextDirection === (sign > 0 ? "uptrend" : "downtrend"), trend = f.regime === (sign > 0 ? "uptrend" : "downtrend");
  return { priceTier, contextAligned: context, trendAligned: trend, momentum: momentumAligned && !momentumOpposed ? "supports" : momentumOpposed ? "conflicts" : "not_confirmed", retracementConfluence: location, rank: priceTier * 3 + (context ? 2 : 0) + (trend ? 1 : 0) + (momentumAligned && !momentumOpposed ? 1 : 0) + (location ? 1 : 0) };
}
function evidenceReview(a) {
  const f = a.frames.find((f2) => f2.role === "setup");
  if (!f?.eligible) return [];
  const v = f.values, obs = f.chartEvidence?.observations || [], fib = swingRetracement(f), titles = (k) => obs.filter((e) => k.includes(e.kind)).map((e) => evidenceTitle(e) + " \xB7 " + e.status), context = a.frames.filter((f2) => f2.role === "context").map((f2) => f2.interval + " " + f2.regime).join(" / ");
  const rows = [
    { family: "Price & boundaries", role: "Primary confirmation", detail: titles(["triangle", "rising_wedge", "falling_wedge", "channel", "failed_breakout", "trendline_reclaim", "zone_rejection", "head_shoulders", "double_reversal", "triple_reversal", "candle_rejection", "ema_pullback"]).join("; ") || "No qualifying completed price response. A forming shape or oscillator reading alone is not an entry." },
    { family: "Trend & higher timeframes", role: "Context and conflict check", detail: `${a.interval}: ${f.regime}. ${context || "No directional higher-timeframe confirmation"}. ${f.structure?.state === "transition" ? "CHOCH is a caution, not a universal veto. Trend continuation is not established; a conditional reversal still needs its own completed price evidence." : "Protected structure and EMA alignment are checked separately."}` },
    { family: "RSI & MACD", role: "One momentum family", detail: `${finite4(v.rsi) ? "RSI14 " + v.rsi.toFixed(1) + ". " : ""}${titles(["rsi_divergence", "macd_cross", "macd_divergence"]).join("; ") || "No fresh confirmed divergence or MACD cross."} ${finite4(v.macd) && finite4(v.macdsignal) ? "MACD " + p(v.macd) + " / signal " + p(v.macdsignal) + ". " : ""}These correlated readings are not independent votes.` },
    { family: "Bollinger & volatility", role: "Regime-specific context", detail: titles(["band_reentry", "volatility_squeeze"]).join("; ") || `No qualified re-entry or contraction. ${finite4(v.bbupper) && finite4(v.bblower) ? "Completed close is " + (v.close > v.bbupper ? "above the upper" : v.close < v.bblower ? "below the lower" : "inside the") + " Bollinger(20,2) band. " : ""}${finite4(v.bbupper) && finite4(v.bblower) ? "Lower / middle / upper: " + p(v.bblower) + " / " + p(v.bbmid) + " / " + p(v.bbupper) + ". " : ""}An outside-band close can accompany continuation; a band touch is not a reversal.` },
    { family: "Fibonacci location", role: "Confluence only", detail: fib ? `${fib.side === "long" ? "Rising" : "Falling"} confirmed swing ${p(fib.start.price)} \u2192 ${p(fib.end.price)}. ${fib.nearRatio === null ? "No 38.2 / 50 / 61.8 / 78.6% level is within 0.2 ATR of the close." : "Close is within 0.2 ATR of the " + (fib.nearRatio * 100).toFixed(1) + "% retracement."} Levels: ${fib.levels.map((l) => (l.ratio * 100).toFixed(1) + "% " + p(l.price)).join(" / ")}. A price reaction and a valid stop/target are still required.` : "No recent qualified confirmed swing leg at the current price; no Fibonacci confluence is claimed." },
    { family: "Recorded volume", role: "Participation context", detail: finite4(v.relativeVolume) ? `Relative recorded volume ${v.relativeVolume.toFixed(2)}\xD7. Coverage is limited to this source; it is not order-book depth or measured volume-at-price. Volume alone does not choose direction.` : "Comparable recorded volume is unavailable; no volume confirmation is claimed." }
  ];
  return rows;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analyst-whatif.js
var WATCH_POLICY = "conditional-watch-v1";
var good = Number.isFinite;
var p2 = (n) => Number(n.toPrecision(8));
function buildWhatIf(a) {
  const f = a.frames.find((f2) => f2.role === "setup"), unavailable = !f?.eligible || a.stance === "insufficient_evidence" || a.frames.some((f2) => !f2.eligible) || a.contradictions.length > 0;
  const base = { policy: WATCH_POLICY, cutoff: a.decisionCutoff, expiresAt: a.decisionCutoff + (f?.period || intervalMs(a.interval)) * a.profile.ttlBars, interval: a.interval, quote: a.quote, status: unavailable ? "source_blocked" : "watch_only", review: evidenceReview(a), retracement: swingRetracement(f), cards: [] };
  if (unavailable) return { ...base, recovery: "Recover compatible completed source/context candles and refresh analysis before deriving entry conditions. Displayed reference levels are not a validated position plan." };
  const obs = f.chartEvidence?.observations || [], v = f.values;
  for (const side of ["long", "short"]) {
    if (a.request.side && a.request.side !== side) continue;
    const sign = side === "long" ? 1 : -1, s = a.scenarios.find((s2) => s2.side === side), level2 = s?.level ?? (side === "long" ? v.resistance20 : v.support20);
    if (!good(level2) || level2 <= 0) continue;
    const observed = obs.filter((e) => e.side === sign).map((e) => evidenceTitle(e) + " \xB7 " + e.status), blockedBy = s ? [...s.contradictions] : ["No qualifying completed price pattern for this direction yet."];
    if (!s && a.contextDirection && a.contextDirection !== (sign > 0 ? "uptrend" : "downtrend")) blockedBy.push("The completed higher timeframe opposes this direction; refresh after its context changes.");
    if (a.request.modifier === "conservative" && !a.contextDirection && !blockedBy.some((x) => x.startsWith("Conservative"))) blockedBy.push("Conservative mode needs aligned directional higher-timeframe context.");
    let risk = null;
    if (s) {
      const riskSize = Math.abs(s.entry - s.stop);
      risk = { entry: s.entry, stop: s.stop, target: s.targets.at(-1), grossR: s.grossR, minR: a.profile.minNetR, requiredTarget: s.entry + sign * riskSize * a.profile.minNetR, passes: s.grossR >= a.profile.minNetR && s.netR !== null ? s.netR >= a.profile.minNetR : s.grossR >= a.profile.minNetR };
    }
    const steps = [
      `Wait for a NEW completed ${a.interval} close ${sign > 0 ? "above" : "below"} ${p2(level2)} ${a.quote}; a wick touch does not qualify.`,
      `Then require a DIFFERENT later ${a.interval} bar to retest the fixed level and ${sign > 0 ? "hold above" : "remain below"} it within ${a.profile.retestBars} bars.`,
      ...blockedBy.length ? ["Refresh analysis to resolve every blocker below; the break and retest alone do not override context, conflicting patterns or poor reward/risk."] : ["Refresh the completed source to confirm the pattern remains valid before inspecting entry, stop and target."],
      `Use a confirmed source extreme for the stop and the nearest recorded opposing level for the target; require at least ${a.profile.minNetR}R gross and recheck net R with costs. Never tighten a stop arbitrarily or move a target through an obstacle.`
    ];
    const momentum2 = obs.filter((e) => e.side === sign && ["rsi_divergence", "macd_cross", "macd_divergence"].includes(e.kind));
    const alternative = `Other possible evidence on ${a.interval}: a confirmed ${sign > 0 ? "lower price low with a higher RSI14 low" : "higher price high with a lower RSI14 high"}, a MACD momentum shift, or a verified channel/trendline/Fibonacci reaction. These are alternatives to investigate, not requirements that must all occur, and none is claimed in advance. Price confirmation and risk gates still apply.`;
    const cancel = `Abandon this watch after a failed retest back ${sign > 0 ? "below" : "above"} ${p2(level2)}${s ? " or a move through the recorded invalidation " + p2(s.stop) : ""}. Refresh on a new confirmed swing, quote/source change, or expiry; fixed levels do not follow a moving channel automatically.`;
    base.cards.push({ side, kind: s ? "existing_pattern" : "boundary_watch", pattern: s?.patternTitle || null, level: level2, levelBasis: s ? "Current recorded pattern trigger" : "Prior 20 completed source-bar " + (sign > 0 ? "resistance" : "support"), observed, steps, blockedBy, risk, cancel, momentumObserved: momentum2.length > 0, alternative, eligibleNow: s?.eligible === true });
  }
  return base;
}
function intervalMs(tf) {
  const m = /^(\d+)([smhdw])$/.exec(tf);
  return m ? Number(m[1]) * { s: 1e3, m: 6e4, h: 36e5, d: 864e5, w: 6048e5 }[m[2]] : 0;
}
function whatIfExplanation(a, { now = Date.now() } = {}) {
  const w = a.whatIf || buildWhatIf(a);
  if (w.status === "source_blocked") return "### What if?\n\n" + w.recovery;
  if (now >= w.expiresAt) return "### What if?\n\nThis saved watch has expired. Refresh analysis for current source levels.";
  const out = ["### What if? \xB7 conditions to reconsider", "These are future checklists, not predictions or confirmed entries. A fresh completed-source analysis must pass every blocker."];
  for (const c of w.cards) {
    out.push(`#### ${c.side === "long" ? "Upside" : "Downside"} route \xB7 ${a.interval}`, `**Watch level:** ${p2(c.level)} ${a.quote}. ${c.levelBasis}.`, c.observed.length ? "**Already observed:** " + c.observed.join("; ") : "**Already observed:** no qualifying directional price pattern yet.", c.steps.slice(0, 3).map((s, i) => i + 1 + ". " + s).join("\n"));
    if (c.risk && !c.risk.passes) out.push(`**Risk barrier:** entry ${p2(c.risk.entry)}, stop ${p2(c.risk.stop)}, nearest target ${p2(c.risk.target)} gives ${c.risk.grossR.toFixed(2)}R gross. At those same entry/stop prices, ${c.risk.minR}R needs ${p2(c.risk.requiredTarget)}. Do not ignore the nearer obstacle: wait for new confirmed geometry and recalculate.`);
    if (c.blockedBy.length) out.push("**Still blocked:** " + c.blockedBy.join(" "));
    out.push("**Cancel:** " + c.cancel);
  }
  out.push("RSI/MACD, Bollinger, trendline/channel reactions and confirmed swing retracements are reviewed as different evidence roles; no single indicator is mandatory. Momentum alone does not confirm an entry.");
  return out.join("\n\n");
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/strategy-candidates.js
var p3 = (x) => Number(x.toPrecision(8));
function evidenceCandidates(f, profile) {
  const observations = f.chartEvidence?.observations || [], last = f.sourceCandles.at(-1), atr = f.values.atr, buffer = atr * profile.stopBuffer, proposals = [];
  for (const side of ["long", "short"]) {
    const sign = side === "long" ? 1 : -1, available = observations.filter((e) => e.side === sign || e.side === 0 && ["triangle", "rising_wedge", "falling_wedge"].includes(e.kind));
    for (const e of available) {
      if (["macd_cross", "macd_divergence", "volatility_squeeze"].includes(e.kind)) continue;
      const forming = e.side === 0, level2 = forming ? sign > 0 ? Math.max(e.upper, last.high) : Math.min(e.lower, last.low) : e.level;
      const anchor = forming ? sign > 0 ? e.lower : e.upper : e.stopAnchor, entry = level2 + sign * buffer, stop = anchor - sign * buffer, risk = sign * (entry - stop);
      if (risk <= 0 || entry <= 0 || stop <= 0) continue;
      const projection = entry + sign * risk * 2;
      if (projection <= 0) continue;
      const pivots = [f.structure.latestHigh, f.structure.latestLow, f.structure.swing?.latestHigh, f.structure.swing?.latestLow].filter(Boolean), barriers = pivots.filter((q) => q.kind === (sign > 0 ? "high" : "low") && sign * (q.price - entry) > atr * 0.05).map((q) => q.price);
      if (e.target > 0 && sign * (e.target - entry) > 0) barriers.push(e.target);
      const nearest = barriers.sort((a, b) => sign * (a - b))[0], target = nearest !== void 0 && sign * (nearest - projection) < 0 ? nearest : projection;
      const supporting = available.filter((x) => x !== e && x.side === sign), opposing = observations.filter((x) => x.side === -sign && x.status !== "forming" && !["macd_cross", "macd_divergence", "rsi_divergence"].includes(x.kind));
      const reasons = [evidenceTitle(e) + " \xB7 " + e.status.replaceAll("_", " ")];
      if (e.kind === "rsi_divergence") reasons.push(`Price ${p3(e.priceA)} \u2192 ${p3(e.priceB)}; RSI14 at those same confirmed price pivots ${e.oscillatorA.toFixed(1)} \u2192 ${e.oscillatorB.toFixed(1)}. Divergence warns of weakening momentum; it does not confirm reversal.`);
      else if (["failed_breakout", "trendline_reclaim"].includes(e.kind)) reasons.push(`A completed close crossed ${p3(e.priceA)}, followed by a completed reclaim. A wick alone was not counted as a failed close breakout.`);
      else if (e.kind === "zone_rejection") reasons.push(`${e.touches} separated confirmed price tests in ${p3(e.priceA)}\u2013${p3(e.priceB)}. This is a price-tested zone, not measured volume-at-price or resting orders.`);
      else if (["double_reversal", "head_shoulders", "triple_reversal"].includes(e.kind)) reasons.push(`${e.touches} confirmed reversal turns; a completed neckline close break at ${p3(e.upper)}. The head/turns must follow a recorded prior move. The measured-height projection is an objective, not a forecast.`);
      else if (e.kind === "band_reentry") reasons.push(`A completed close moved outside Bollinger 20/2, then returned inside in a non-directional EMA context. The middle band at ${p3(e.priceB)} is the recorded mean-reversion objective.`);
      else if (e.kind === "ema_pullback") reasons.push(`An active protected trend retested EMA20 ${p3(e.priceA)} and closed away. The confirmed protected swing ${p3(e.priceB)} still holds.`);
      else if (e.kind === "candle_rejection") reasons.push(`A directional engulfing/long-wick candle rejected an already confirmed price pivot ${p3(e.priceA)}. The candle alone is not the entry signal.`);
      else reasons.push(`Confirmed swing boundaries: lower ${p3(e.lower)}, upper ${p3(e.upper)} at the source cutoff. ${e.touches} boundary anchors. The geometry alone does not predict the break direction.`);
      const groups3 = new Set(available.filter((x) => x.side === sign || x === e).map((x) => ["rsi_divergence", "macd_cross", "macd_divergence"].includes(x.kind) ? "momentum" : "price"));
      for (const m of observations.filter((x) => x.side === -sign && ["macd_cross", "macd_divergence", "rsi_divergence"].includes(x.kind))) reasons.push("Counterevidence: " + evidenceTitle(m) + " warns against this direction; follow-through is required.");
      for (const m of supporting.filter((x) => ["macd_cross", "macd_divergence"].includes(x.kind))) reasons.push(evidenceTitle(m) + " \xB7 momentum context, not an independent price-structure vote.");
      proposals.push({
        family: "breakout_retest",
        side,
        level: level2,
        entry,
        stop,
        target,
        phase: "waiting_breakout",
        patternTitle: evidenceTitle(e),
        patternKind: e.kind,
        evidenceIds: [e.kind + ":" + e.first + ":" + e.last, ...supporting.map((x) => x.kind + ":" + x.first + ":" + x.last)],
        patternEvidence: reasons,
        entryBasis: `A fresh completed close ${sign > 0 ? "above" : "below"} ${p3(level2)} must be followed by a separate retest within ${profile.retestBars} bars. The level is a fixed snapshot; moving boundaries require a refreshed analysis.`,
        stopBasis: `Beyond the ${forming ? "opposite confirmed pattern boundary" : "recorded pattern extreme"} ${p3(anchor)} plus ${p3(buffer)} ${f.selection.quote} ATR buffer.`,
        targetBasis: target === nearest ? target === e.target && ["double_reversal", "head_shoulders", "triple_reversal"].includes(e.kind) ? "Pattern-height projection; capped if a nearer opposing confirmed swing is recorded." : "Nearest recorded opposing swing or channel/band objective; reward is capped before that obstacle." : "Modeled 2R projection; no nearer opposing confirmed swing was found in this bounded window.",
        reason: `Require a new completed ${f.interval} close ${sign > 0 ? "above" : "below"} ${p3(level2)}, then a later retest that ${sign > 0 ? "holds" : "fails to reclaim"} it. No market entry is confirmed now.`,
        invalidates: `Cancel before confirmation on a completed close back ${sign > 0 ? "below" : "above"} ${p3(level2)} after the break, any trade through stop ${p3(stop)}, or expiry. Re-evaluate if source structure changes.`,
        patternRank: (e.status === "forming" ? 1 : 3) + groups3.size,
        opposingEvidence: opposing.map((x) => evidenceTitle(x) + " on the same completed timeframe."),
        patternCutoff: e.confirmedAt
      });
    }
  }
  return proposals;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/technical-analysis.js
var LEVEL_POLICY = "confirmed-level-context-v1";
var TA_REFERENCE = Object.freeze({ title: "Arkham \xB7 Crypto Trading 101: Technical Analysis", url: "https://info.arkm.com/research/crypto-trading-101-technical-analysis", publishedAt: "2023-12-05", purpose: "Educational concepts; observed levels and thresholds are Ticking calculations, not Arkham signals." });
var finite5 = Number.isFinite;
function levelContext(f) {
  const bars = f.sourceCandles || [], last = bars.at(-1), atr = f.values?.atr;
  const result = { policy: LEVEL_POLICY, interval: f.interval, quote: f.selection?.quote || "", eligible: !!f.eligible, scope: "retained completed source window; not all-time or whole-market coverage", zones: [] };
  if (!f.eligible || !last || !finite5(atr) || atr <= 0) return result;
  for (const kind of ["low", "high"]) {
    const raw = [...(f.structure?.swing?.pivots || []).map((p4) => ({ ...p4, scale: "structural" })), ...(f.structure?.internal?.pivots || []).map((p4) => ({ ...p4, scale: "internal" }))];
    const seen = /* @__PURE__ */ new Set(), pivots = raw.filter((p4) => {
      const key = p4.kind + ":" + p4.time;
      if (p4.kind !== kind || p4.confirmedAt > last.time || seen.has(key) || p4.index < 0 || p4.index >= bars.length) return false;
      seen.add(key);
      return true;
    }).sort((a, b) => a.time - b.time);
    const clusters = [];
    for (const seed of pivots.slice(-16)) {
      const near = pivots.filter((p4) => Math.abs(p4.price - seed.price) <= atr * 0.175), tests = [];
      for (const p4 of near) if (!tests.length || p4.index - tests.at(-1).index >= 6) tests.push(p4);
      if (tests.length < 2 || last.time - tests.at(-1).time > 180 * f.period) continue;
      const lower = Math.min(...tests.map((p4) => p4.price)), upper = Math.max(...tests.map((p4) => p4.price)), price2 = kind === "high" ? upper : lower;
      const knownAt = Math.max(...tests.map((p4) => p4.confirmedAt)), sign = kind === "high" ? 1 : -1, tolerance = atr * 0.15, allowance = atr * 0.05;
      let phase = "unbroken", breakAt = null, retestAt = null, failedAt = null, sweepAt = null;
      for (const bar of bars) {
        if (bar.time < knownAt) continue;
        const outside = sign * (bar.close - price2) > allowance;
        if (breakAt === null || phase === "failed_close_break") {
          if (outside) {
            phase = "closed_break";
            breakAt = bar.time;
            retestAt = null;
            failedAt = null;
          } else if (sign * ((sign > 0 ? bar.high : bar.low) - price2) > allowance) sweepAt = bar.time;
        } else if (sign * (bar.close - price2) < -allowance) {
          phase = "failed_close_break";
          failedAt = bar.time;
        } else if (bar.time > breakAt && (sign > 0 ? bar.low <= upper + tolerance && bar.low >= lower - tolerance : bar.high >= lower - tolerance && bar.high <= upper + tolerance) && sign * (bar.close - price2) >= 0) {
          phase = "retest_holding";
          retestAt = bar.time;
        }
      }
      if (phase === "unbroken" && sweepAt !== null) phase = "wick_sweep";
      const zone = { kind: kind === "high" ? "resistance" : "support", lower, upper, price: price2, knownAt, tests: tests.length, structuralTests: tests.filter((p4) => p4.scale === "structural").length, anchors: tests.slice(-12).map((p4) => ({ time: p4.time, price: p4.price, confirmedAt: p4.confirmedAt, scale: p4.scale })), phase, breakAt, retestAt, failedAt, sweepAt, tolerance, allowance };
      if (!clusters.some((z) => Math.abs(z.price - price2) <= atr * 0.175)) clusters.push(zone);
    }
    const best = clusters.sort((a, b) => b.structuralTests - a.structuralTests || b.tests - a.tests || Math.abs(last.close - a.price) - Math.abs(last.close - b.price) || b.knownAt - a.knownAt)[0];
    if (best) result.zones.push(best);
  }
  return result;
}
function levelNotes(a, format = String) {
  return a.frames.filter((f) => f.levelContext?.eligible).flatMap((f) => f.levelContext.zones.map((z) => {
    const state = { unbroken: "No completed close break after the level became known.", wick_sweep: "A wick crossed the level; no completed close break was confirmed.", closed_break: "A completed close broke the level; a separate later successful retest is not recorded.", retest_holding: "A different later bar retested the zone and closed on the breakout side. This describes history, not an executed entry.", failed_close_break: "A completed close broke the level, then a later close returned through it. The old breakout is invalidated; refresh for a new setup." }[z.phase];
    return `${f.interval} ${z.kind} ${format(z.lower)}\u2013${format(z.upper)} ${a.quote}: ${z.tests} separated confirmed tests${z.structuralTests ? " (" + z.structuralTests + " structural)" : ""}. ${state}`;
  }));
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/agent-chart-tools.js
var finite6 = (x) => typeof x === "number" && Number.isFinite(x);
var MAX_BYTES = 2 * 1024 * 1024;
function check(ok, message) {
  if (!ok) throw Error(message);
}
function fields(o, keys) {
  check(o && typeof o === "object" && !Array.isArray(o), "Expected an object.");
  check(Object.keys(o).every((k) => keys.includes(k)), "Unknown field.");
}
function num(x, min, max, label) {
  check(finite6(x) && x >= min && x <= max, `Invalid ${label}.`);
  return x;
}
function integer(x, min, max, label) {
  num(x, min, max, label);
  check(Number.isInteger(x), `Invalid ${label}.`);
  return x;
}
function planPosition(spec) {
  fields(spec, ["side", "entry", "stop", "targets", "riskBudget", "quantity", "feeBps", "slippageBps", "quantityStep"]);
  check(["long", "short"].includes(spec.side), "Choose long or short.");
  const sign = spec.side === "long" ? 1 : -1;
  for (const k of ["entry", "stop"]) num(spec[k], Number.MIN_VALUE, 1e18, k);
  check(sign * (spec.entry - spec.stop) > 0, "Stop must be below a long entry or above a short entry.");
  check(Array.isArray(spec.targets) && spec.targets.length > 0 && spec.targets.length <= 5, "Use 1\u20135 alternative full-exit targets.");
  spec.targets.forEach((p4) => {
    num(p4, Number.MIN_VALUE, 1e18, "target");
    check(sign * (p4 - spec.entry) > 0, "Targets must be on the profit side.");
  });
  check(spec.riskBudget !== void 0 !== (spec.quantity !== void 0), "Supply riskBudget OR base quantity.");
  const f = num(spec.feeBps ?? 0, 0, 1e3, "fee basis points") / 1e4, s = num(spec.slippageBps ?? 0, 0, 1e3, "slippage basis points") / 1e4;
  const entry = spec.entry * (1 + sign * s), exit = (p4) => p4 * (1 - sign * s), net = (p4) => sign * (exit(p4) - entry) - f * (entry + exit(p4));
  const unitRisk = -net(spec.stop);
  check(unitRisk > 0 && finite6(unitRisk), "Invalid unit risk.");
  let quantity = spec.quantity !== void 0 ? num(spec.quantity, Number.MIN_VALUE, 1e18, "quantity") : num(spec.riskBudget, Number.MIN_VALUE, 1e18, "risk budget") / unitRisk;
  if (spec.quantityStep !== void 0) {
    const step = num(spec.quantityStep, Number.MIN_VALUE, 1e18, "quantity step");
    quantity = Math.floor(quantity / step) * step;
  }
  check(finite6(quantity) && quantity > 0 && finite6(quantity * entry) && quantity * entry <= 1e24, "Quantity is zero, excessive or invalid.");
  const risk = quantity * unitRisk;
  return { model: "linear_quote_cash_scenario", side: spec.side, quantity, entryAfterSlippage: entry, stopAfterSlippage: exit(spec.stop), notional: quantity * entry, riskAtAssumedStop: risk, targets: spec.targets.map((price2) => ({ price: price2, exitAfterSlippage: exit(price2), netPnl: quantity * net(price2), rewardRisk: net(price2) / unitRisk })), assumptions: { feeBps: spec.feeBps ?? 0, slippageBps: spec.slippageBps ?? 0, targets: "alternative full exits, not partial allocations", excludes: ["funding", "borrow", "liquidation", "nonlinear contracts", "gaps beyond assumed slippage", "actual liquidity and fill validation"] }, execution: "no_order" };
}
function analyzeStructure(snapshot, { left = 3, right = 3, compact = false } = {}) {
  integer(left, 1, 20, "left bars");
  integer(right, 1, 20, "right bars");
  const bars = sourceSegment(snapshot.candles, snapshot.timing.intervalMs, snapshot.selection.quote).bars;
  const structure = marketStructure(bars, { period: snapshot.timing.intervalMs, left, right });
  return { ...compact ? compactStructure(structure) : structure, regimeSegmentStart: bars[0]?.time ?? 0, quality: snapshot.quality };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analyst-core.js
var ANALYST_VERSION = "source-analyst-v3";
var TARGET_POLICY = "evidence-opposing-levels-v3.2";
var FEATURES = [{ id: "ema", uid: "e20", period: 20 }, { id: "ema", uid: "e50", period: 50 }, { id: "rsi", uid: "r14", period: 14 }, { id: "atr", uid: "a14", period: 14 }, { id: "bb", uid: "bb20", period: 20, multiplier: 2 }, { id: "adx", uid: "adx14", period: 14 }, { id: "macd", uid: "macd" }, { id: "tickvolume", uid: "rv20", period: 20 }, { id: "vwap", uid: "vwap" }];
var PROFILES = Object.freeze({ scalping: { maxExtension: 0.6, stopBuffer: 0.15, minNetR: 1.5, ttlBars: 12, retestBars: 4 }, intraday: { maxExtension: 1, stopBuffer: 0.25, minNetR: 1.8, ttlBars: 24, retestBars: 6 }, swing: { maxExtension: 1.5, stopBuffer: 0.4, minNetR: 2, ttlBars: 36, retestBars: 10 } });
var valid2 = Number.isFinite;
var price = (x) => Number(x.toPrecision(8));
var stamp2 = (t) => new Date(t).toISOString();
function canonical(value) {
  return Array.isArray(value) ? value.map(canonical) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().filter((k) => value[k] !== void 0).map((k) => [k, canonical(value[k])])) : value;
}
function fingerprint(value) {
  let h = 2166136261;
  for (const c of JSON.stringify(canonical(value))) {
    h ^= c.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}
function frameRevision(frame) {
  return fingerprint([frame.identity, frame.interval, frame.quote, frame.candles.map((b) => [b.time, b.open, b.high, b.low, b.close, b.volume, b.quoteCurrency, b.synthetic, b.estimated, b.sourceCandleState, b.quoteCurrencies, b.referenceOnly, b.mixedSource])]);
}
function frameFeatures(frame, cutoff) {
  const period = parseInterval(frame.interval).milliseconds, s = analyzeMarket(frame, { now: cutoff, maxAgeMs: Math.min(864e5, Math.max(12e4, period * 1.1)), studies: FEATURES, includeCandles: true, candleLimit: 600 });
  const bars = sourceSegment(s.candles, period, s.selection.quote).bars, calc = computeIndicators(bars, normalizeIndicators(FEATURES), period, { strict: true });
  const line = (id, key = "value") => calc.find((x) => x.uid === id)?.lines[key] || [], last = (id, key) => line(id, key).at(-1) ?? null;
  const structure = analyzeStructure({ ...s, candles: bars.map((b2) => b2.mixedSource ? { ...b2, mixedSource: false } : b2) }, { compact: false }), prior = bars.slice(-21, -1), b = bars.at(-1), v = { close: b?.close ?? null, ema20: last("e20"), ema50: last("e50"), ema20prev3: line("e20").at(-4) ?? null, rsi: last("r14"), atr: last("a14"), bbmid: last("bb20", "middle"), bbupper: last("bb20", "upper"), bblower: last("bb20", "lower"), adx: last("adx14"), pdi: last("adx14", "positive"), mdi: last("adx14", "negative"), macd: last("macd"), macdsignal: last("macd", "signal"), macdhist: last("macd", "histogram"), relativeVolume: last("rv20"), vwap: last("vwap"), support20: prior.length === 20 ? Math.min(...prior.map((x) => x.low)) : null, resistance20: prior.length === 20 ? Math.max(...prior.map((x) => x.high)) : null };
  const eligible = !s.quality.blockers.filter((x) => x !== "mixed_source_bar").length && bars.length >= 50 && valid2(v.atr) && v.atr > 0;
  return { retracement: eligible ? deriveSwingRetracement({ ...s, analysisATR: v.atr }, bars, period) : null, interval: frame.interval, period, revision: frameRevision({ ...frame, candles: s.candles }), guardRevision: frameRevision({ ...frame, candles: s.candles.slice(-50) }), selection: s.selection, quality: s.quality, bars: bars.length, lastTime: b?.time ?? null, segmentStart: bars[0]?.time ?? null, values: v, structure, chartEvidence: chartEvidence(bars, period), studies: s.studies, sourceCandles: bars, calculationVersion: CALCULATION_VERSION, eligible, derivation: frame.derivation || null, contextSource: frame.contextSource || null, origin: "canonical_js" };
}
function regime(f) {
  const v = f.values;
  if (!f.eligible) return "unavailable";
  if (f.structure?.state === "transition") return "mixed";
  const distance = (v.ema20 - v.ema50) / v.atr, slope2 = (v.ema20 - v.ema20prev3) / v.atr, structure = f.structure?.regime;
  if (distance > 0.1 && slope2 > 0.02 && v.close > v.ema20 && structure === "rising") return "uptrend";
  if (distance < -0.1 && slope2 < -0.02 && v.close < v.ema20 && structure === "falling") return "downtrend";
  return valid2(v.adx) && v.adx < 20 && Math.abs(distance) < 0.5 ? "range" : "mixed";
}
function scenario({ family, side, level: level2, entry, stop, target, phase, reason, invalidates, f, profile, cutoff }) {
  const sign = side === "long" ? 1 : -1;
  if (![level2, entry, stop, target].every((x) => valid2(x) && x > 0) || sign * (entry - stop) <= 0 || sign * (target - entry) <= 0) return null;
  const first = entry + sign * Math.abs(entry - stop), targets = first > 0 && sign * (target - first) > Math.max(entry, target) * 1e-12 ? [first, target] : [target];
  return { quote: f.selection.quote, id: family + "-" + side, kind: family, side, level: level2, entry, stop, targets, phase, status: phase.replaceAll("_", " "), reason, condition: reason, invalidation: invalidates, invalidates, confirmedAt: cutoff, sourceRevision: f.revision, expiresAt: cutoff + f.period * profile.ttlBars, setup: { version: 1, kind: family, side, level: level2, entry, stop, target, createdCutoff: cutoff, ttlBars: profile.ttlBars, retestBars: profile.retestBars, buffer: f.values.atr * profile.stopBuffer, sourceRevision: f.revision, ruleVersion: ANALYST_VERSION }, grossR: Math.abs(target - entry) / Math.abs(entry - stop), evidence: [], contradictions: [] };
}
function costScenario(s, costs = {}) {
  const known = valid2(costs.feeBps) && valid2(costs.slippageBps), spec = { side: s.side, entry: s.entry, stop: s.stop, targets: s.targets, feeBps: costs.feeBps ?? 0, slippageBps: costs.slippageBps ?? 0, ...valid2(costs.riskBudget) ? { riskBudget: costs.riskBudget } : { quantity: 1 } };
  try {
    if (costs.riskQuote && s.quote && costs.riskQuote !== s.quote) throw Error("Risk budget quote " + costs.riskQuote + " differs from source " + s.quote + "; no conversion was inferred.");
    const result = planPosition(spec);
    return { ...s, costsKnown: known, costAssumptions: { feeBps: costs.feeBps ?? null, slippageBps: costs.slippageBps ?? null, riskBudget: costs.riskBudget ?? null }, costModel: result, netR: known ? result.targets.at(-1).rewardRisk : null };
  } catch (e) {
    return { ...s, costsKnown: known, netR: null, costError: e.message };
  }
}
function analyzeFrames({ frames, features, request, cutoff = Date.now(), analysisId }) {
  if (!Array.isArray(frames) || !frames.length || frames.length > 4) throw Error("Use one setup and at most three context/trigger frames.");
  const all = frames.map((frame, i) => {
    const local = features?.[i] || frameFeatures(frame, cutoff);
    return { ...local, levelContext: levelContext(local), regime: regime(local) };
  });
  const main = all.find((f) => f.interval === request.interval);
  if (!main) throw Error("The requested setup interval is missing.");
  const contexts = (request.contextIntervals || []).map((tf) => all.find((f) => f.interval === tf)).filter(Boolean), trigger = request.triggerInterval ? all.find((f) => f.interval === request.triggerInterval) : null;
  const quote = main.selection.quote, conflicts = [], missing = [];
  for (const tf of [...request.contextIntervals || [], ...request.triggerInterval ? [request.triggerInterval] : []]) if (!all.some((f) => f.interval === tf)) missing.push(tf + ": requested retained frame unavailable");
  for (const f of all) {
    if (f.selection.quote !== quote) conflicts.push(f.interval + " has a different quote; it cannot confirm this setup.");
    if (!f.eligible) missing.push(f.interval + ": " + (f.quality.blockers.join(", ") || "fewer than 50 eligible consecutive completed bars"));
  }
  const usable = contexts.filter((f) => f.eligible && f.selection.quote === quote), directions = usable.map((f) => f.regime).filter((x) => ["uptrend", "downtrend"].includes(x));
  const contextDirection = directions.length && directions.every((x) => x === directions[0]) ? directions[0] : null;
  if (new Set(directions).size > 1) conflicts.push("Higher timeframes disagree on direction.");
  const style = request.style || "intraday";
  if (!PROFILES[style]) throw Error("Choose scalping, intraday or swing.");
  const profile = { ...PROFILES[style], ...request.modifier === "conservative" ? { maxExtension: PROFILES[style].maxExtension * 0.75, minNetR: PROFILES[style].minNetR + 0.5 } : {} };
  const v = main.values, candidates = [], last = main.sourceCandles?.at(-1) || frames.find((x) => x.interval === request.interval)?.candles.filter((b) => b.time + main.period <= cutoff).at(-1);
  if (main.eligible) {
    for (const spec of evidenceCandidates(main, profile)) {
      const candidate = scenario({ ...spec, f: main, profile, cutoff });
      if (candidate) candidates.push({ ...candidate, ...Object.fromEntries(["patternTitle", "patternKind", "evidenceIds", "patternEvidence", "entryBasis", "stopBasis", "targetBasis", "patternRank", "opposingEvidence", "patternCutoff"].map((k) => [k, spec[k]])) });
    }
  }
  const checked = candidates.map((s) => {
    const sign = s.side === "long" ? 1 : -1, opposing = usable.flatMap((f) => (f.levelContext?.zones || []).filter((z) => z.kind === (sign > 0 ? "resistance" : "support") && !["retest_holding", "closed_break"].includes(z.phase)).map((z) => ({ interval: f.interval, price: sign > 0 ? z.lower : z.upper, tests: z.tests }))).filter((z) => sign * (z.price - s.entry) > 0 && sign * (s.targets.at(-1) - z.price) > 0).sort((a, b) => sign * (a.price - b.price));
    if (opposing.length) {
      const barrier = opposing[0];
      s = { ...s, targets: s.targets.filter((t) => sign * (t - barrier.price) < 0).concat(barrier.price), setup: { ...s.setup, target: barrier.price }, grossR: Math.abs(barrier.price - s.entry) / Math.abs(s.entry - s.stop), targetBasis: `Capped at ${barrier.interval} confirmed ${sign > 0 ? "resistance" : "support"} ${price(barrier.price)} ${quote} (${barrier.tests} separated source tests). The nearer opposing source zone is not skipped to preserve a larger reward/risk.` };
    }
    const aligned = contextDirection ? contextDirection === (s.side === "long" ? "uptrend" : "downtrend") : true, trendAligned = main.regime === (s.side === "long" ? "uptrend" : "downtrend"), result = costScenario(s, request.costs);
    if (s.opposingEvidence?.length) result.contradictions.push("Conflicting completed pattern evidence: " + s.opposingEvidence.join(" "));
    result.warnings = main.structure?.state === "transition" ? ["Structure is transitioning; no completed trend reversal is claimed. This conditional price setup needs its own new close and later retest."] : [];
    if (main.structure?.state === "transition" && s.patternKind === "ema_pullback") result.contradictions.push("Trend continuation requires an active protected trend, not a CHOCH transition.");
    if (!aligned) result.contradictions.push("Opposes the completed higher-timeframe trend.");
    if (request.modifier === "conservative" && !contextDirection) result.contradictions.push("Conservative mode requires an aligned directional context.");
    if (missing.length || conflicts.length || contexts.some((f) => !f.eligible) || trigger && !trigger.eligible) result.contradictions.push("Requested context/trigger evidence is incomplete or incompatible.");
    if (request.side && s.side !== request.side) result.contradictions.push("Not the requested side.");
    if (s.phase === "extended") result.contradictions.push("Move is extended under the selected profile.");
    if (trigger && trigger.eligible && trigger.selection.quote === quote && trigger.regime === (s.side === "long" ? "downtrend" : "uptrend")) result.contradictions.push("The completed trigger timeframe currently opposes this side. Wait for its structure to change.");
    if (result.netR !== null && result.netR < profile.minNetR) result.contradictions.push("Net target reward/risk is below the profile requirement.");
    if (result.costError) result.contradictions.push(result.costError);
    result.factIds = [main.interval + ":close", main.interval + ":ema20", main.interval + ":ema50", main.interval + ":atr", main.interval + ":rsi", main.interval + ":" + (s.side === "long" ? "resistance20" : "support20")];
    result.evidence = [...s.patternEvidence || [], main.interval + " regime: " + main.regime, usable.length ? "Completed context: " + usable.map((f) => f.interval + " " + f.regime).join(", ") : "No directional higher-timeframe confirmation", valid2(v.rsi) ? "Wilder RSI14 " + v.rsi.toFixed(1) + " (momentum evidence, not an independent direction vote)" : "RSI unavailable", valid2(v.relativeVolume) ? "Recorded relative volume " + v.relativeVolume.toFixed(2) + "\xD7" : "Recorded volume confirmation unavailable"];
    result.support = scenarioSupport(main, s, contextDirection);
    result.fitRank = result.support.rank;
    result.watchable = s.grossR >= profile.minNetR && (result.netR === null || result.netR > 0);
    if (s.grossR < profile.minNetR) result.contradictions.push("The nearest opposing price level leaves " + s.grossR.toFixed(2) + "R gross, below the " + profile.minNetR + "R profile minimum.");
    result.eligible = result.contradictions.length === 0 && s.grossR >= profile.minNetR;
    return result;
  }).sort((a, b) => Number(b.side === request.side) - Number(a.side === request.side) || Number(b.eligible) - Number(a.eligible) || Number(b.watchable) - Number(a.watchable) || b.fitRank - a.fitRank || b.patternCutoff - a.patternCutoff || a.patternKind.localeCompare(b.patternKind));
  const evaluated = checked.filter((s, i) => checked.findIndex((t) => t.side === s.side) === i);
  const leaders = evaluated.filter((s) => s.eligible), chosen = leaders.length && !(leaders.length > 1 && leaders[0].fitRank === leaders[1].fitRank) ? leaders[0] : null, costsKnown = valid2(request.costs?.feeBps) && valid2(request.costs?.slippageBps), stance = !main.eligible || missing.length || conflicts.length ? "insufficient_evidence" : !chosen ? "wait" : "conditional_" + chosen.side;
  const assessment = { status: chosen ? "conditional" : "hold", reason: !main.eligible || missing.length || conflicts.length ? "Required source/context evidence is incomplete." : !evaluated.length ? "No current price pattern, protected-trend pullback or range re-entry qualifies for a position plan in this completed source window. Momentum signals alone do not justify entry." : leaders.length > 1 && leaders[0].fitRank === leaders[1].fitRank ? "Opposite directions have equal rule support. Wait for a completed break and retest; no side is preferred." : !chosen ? "Recorded patterns conflict with context, or the nearest opposing level leaves too little reward for the planned stop." : "One conditional plan has the strongest recorded support. Entry is still unconfirmed." };
  const reference = all.some((f) => f.quality.referenceSeries || f.quality.referenceOnlyBars > 0), id = analysisId || "analysis-" + fingerprint([request, all.map((f) => f.revision), ANALYST_VERSION, TARGET_POLICY, DECISION_POLICY, LEVEL_POLICY]);
  const analysis = { analysisReferences: [TA_REFERENCE], levelPolicy: LEVEL_POLICY, decisionPolicy: DECISION_POLICY, candidateAudit: { evaluated: checked.length, bySide: ["long", "short"].map((side) => ({ side, checked: checked.filter((s) => s.side === side).length, eligible: checked.filter((s) => s.side === side && s.eligible).length })), selection: "all proposals pass through context/risk checks before one per side is retained" }, schema: "ticking.analyst.v1", analysisId: id, revision: fingerprint(all.map((f) => f.revision)), targetPolicy: TARGET_POLICY, ruleVersion: ANALYST_VERSION, calculationVersion: CALCULATION_VERSION, symbol: request.symbol, asset: request.asset, interval: main.interval, quote, asOf: main.lastTime === null ? null : stamp2(main.lastTime), decisionCutoff: cutoff, style, profile, assessment, regime: main.regime, contextDirection, stance, preferred: chosen?.side || null, selectedSetupId: chosen?.id || null, scenarios: evaluated, missingInputs: [...missing, ...!costsKnown ? ["Provide fee and slippage assumptions to assess net reward/risk."] : []], contradictions: conflicts, notes: [...request.modifier === "conservative" ? ["Conservative mode reduces allowed extension by 25% and raises minimum net reward/risk by 0.5R; it does not change your risk budget."] : [], ...request.modifier === "tighter_stop" ? ["The stop remains anchored beyond source structure. Moving it closer without a new confirmed swing would change the setup; edit an explicit alternative in the planner."] : [], ...reference ? ["Reference prices are analysis references; these are not executable venue quotes."] : [], "No fill, funding, borrow, liquidation or market impact has been inferred."], facts: all.flatMap((f) => Object.entries(f.values).map(([name, value]) => ({ id: f.interval + ":" + name, interval: f.interval, name, value, quote: ["rsi", "adx", "pdi", "mdi", "relativeVolume"].includes(name) ? null : f.selection.quote, asOf: f.lastTime, revision: f.revision, calculationVersion: f.calculationVersion }))), frames: all.map((f) => ({ interval: f.interval, role: f.interval === request.interval ? "setup" : f.interval === request.triggerInterval ? "trigger" : "context", regime: f.regime, bars: f.bars, revision: f.revision, guardRevision: f.guardRevision, lastTime: f.lastTime, selection: f.selection, quality: f.quality, eligible: f.eligible, values: f.values, structure: compactStructure(f.structure), levelContext: f.levelContext, retracement: f.retracement || null, chartEvidence: f.chartEvidence, calculationVersion: f.calculationVersion, derivation: f.derivation, contextSource: f.contextSource, origin: f.origin, kernelParityVerified: f.kernelParityVerified === true })), bars: main.bars, last: v.close, ema20: v.ema20, ema50: v.ema50, rsi: v.rsi, atr: v.atr, support: v.support20, resistance: v.resistance20, performanceEvidence: { status: "unmeasured", probability: null, description: "Rule fit and source conformance are not a measured probability or profitability estimate." }, request, method: "SMA-seeded EMA20/50; Wilder RSI14/ATR14/ADX14; causal 3/3 internal and 8/8 structural reaction swings; protected levels, completed-close BOS/CHOCH and opposite follow-through confirmation; prior 20-bar levels. Separate context/setup/trigger cutoffs. Causal price-pivot RSI/MACD divergence; SMA-seeded MACD12/26/9 cross; alternating-turn double/triple and head-and-shoulders neckline breaks; four-anchor compression/channels; close/line break reclaim; three-test price zones; protected EMA pullbacks; source-level candle reactions; non-directional Bollinger re-entry and bandwidth contraction. Momentum observations are context, not standalone entries. Evidence-qualified fixed-level breakout/retest plans; stops beyond source extremes with ATR buffers; targets capped at nearer recorded opposing swings, otherwise modeled 2R. CHOCH is context rather than a universal veto; active trend continuation still needs protected structure. All pattern candidates are evaluated before one per side is selected. Completed price reactions lead, followed by context alignment; correlated momentum contributes at most one rank point and confirmed swing Fibonacci proximity is confluence only. Neutral ties, conflicting evidence and inadequate reward/risk stay on hold. Price zones do not measure volume-at-price. Legacy alert setups retain their original evaluator. Repeated horizontal zones use at least two causally confirmed swings, six-bar test spacing and at most 0.35 ATR total width. A wick sweep, completed close break and different-bar retest are separate states. Eligible higher-timeframe opposing zones cap targets before cost/risk checks. Article examples are historical education, never live levels. Thresholds are versioned Ticking policies, not universal trading laws." };
  analysis.whatIf = buildWhatIf(analysis);
  return analysis;
}
function analysisSummary(a) {
  const f = a.frames.find((f2) => f2.role === "setup"), v = f.values, n = (x) => valid2(x) ? price(x) : "unavailable", s = a.scenarios.find((s2) => s2.id === a.selectedSetupId) || a.scenarios[0];
  return `${a.symbol} \xB7 ${a.interval} \xB7 ${a.quote} \xB7 ${a.style}
Stance: ${a.stance.replaceAll("_", " ")}. ${a.contextDirection ? "Higher-timeframe direction: " + a.contextDirection + "." : ""}
${f.structure?.method ? describeStructure(structureContext(f.structure)) + "\n" : ""}Observed ${a.regime}: close ${n(v.close)}, EMA20/50 ${n(v.ema20)} / ${n(v.ema50)}, Wilder RSI14 ${n(v.rsi)}, ATR14 ${n(v.atr)}.
${s ? `${a.stance.startsWith("conditional_") ? "Conditional candidate" : "Watch scenario; entry is not confirmed"}: ${s.kind.replaceAll("_", " ")} \xB7 ${s.side}. ${s.condition}
Entry reference ${n(s.entry)} \xB7 stop ${n(s.stop)} \xB7 target ${n(s.targets.at(-1))}. ${s.netR === null ? "Net reward/risk unavailable until costs are supplied." : "Net reward/risk " + s.netR.toFixed(2) + "R."}
Invalidation: ${s.invalidation}
${s.contradictions.length ? "Counterevidence: " + s.contradictions.join(" ") + "\n" : ""}` : ""}${a.missingInputs.length ? "Missing: " + a.missingInputs.join(" ") + "\n" : ""}${a.contradictions.length ? "Conflicts: " + a.contradictions.join(" ") + "\n" : ""}Stay flat until all selected confirmation conditions occur. ${a.notes.join(" ")}
Source cutoff ${stamp2(a.decisionCutoff)} \xB7 ${a.analysisId}. Predictive win probability is unmeasured.`;
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analysis-visuals.js
var finite7 = Number.isFinite;
function transitionEvidence(f) {
  const s = f?.structure;
  if (s?.state !== "transition") return null;
  const events = s[s.primary]?.events || [], e = events.findLast((e2) => e2.type === "choch");
  if (!e || !Number.isSafeInteger(e.confirmedAt) || !finite7(e.level) || e.level <= 0) return { title: "Structure transition", detail: "The saved structure is transitioning. No timestamped CHOCH event is available in this bounded event record; no precise break is asserted." };
  return { title: "Ongoing structure transition \xB7 " + f.interval, detail: `The transition began when a completed ${f.interval} close broke the protected level ${Number(e.level.toPrecision(7))} ${e.direction} at ${new Date(e.confirmedAt).toISOString().slice(0, 16).replace("T", " ")} UTC. This is a saved context event, not a new signal on every analysis. Trend-continuation rules need active structure; this warning does not automatically disqualify a separate reversal setup.` };
}

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/analyst-presentation.js
var analystPrice = (n) => Number.isFinite(n) ? new Intl.NumberFormat("en-US", { maximumFractionDigits: Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : Math.min(12, Math.max(4, 6 - Math.floor(Math.log10(Math.abs(n) || 1)))) }).format(n) : "\u2014";
function strategyCandidates(a, now = Date.now()) {
  if (a.stance === "insufficient_evidence" || a.frames.some((f) => !f.eligible) || a.contradictions.length) return [];
  return a.scenarios.filter((s) => s.eligible && !s.contradictions.length && s.expiresAt > now).sort((x, y) => y.fitRank - x.fitRank || y.grossR - x.grossR || x.id.localeCompare(y.id));
}
function strategyRationale(a, s) {
  const main = a.frames.find((f) => f.role === "setup"), v = main.values, context = a.frames.filter((f) => f.role === "context");
  if (s.patternTitle) return [...s.patternEvidence || [], s.entryBasis, s.stopBasis, s.targetBasis, `${a.interval}: ${a.regime}. Context: ${context.map((f) => f.interval + " " + f.regime).join(", ") || "no directional confirmation"}.`];
  const lines = [...main.structure?.method ? [describeStructure(structureContext(main.structure))] : [], `${a.interval} structure: ${a.regime}. ${context.length ? "Higher timeframe: " + context.map((f) => f.interval + " " + f.regime).join(", ") + "." : ""}`];
  if (s.kind === "breakout_retest") lines.push(`The prior 20 completed bars define ${s.side === "long" ? "resistance" : "support"} at ${analystPrice(s.level)}. Entry is offset by ${analystPrice(Math.abs(s.entry - s.level))} ${a.quote}; the stop uses ATR14 ${analystPrice(v.atr)} plus the profile buffer. The final target is a modeled ${s.grossR.toFixed(2)}R projection.`);
  else if (s.kind === "trend_pullback") lines.push(`The pullback must preserve a confirmed swing while price returns toward EMA20 ${analystPrice(v.ema20)}. The target uses the recorded opposite 20-bar level; it is not a forecast.`);
  else lines.push("The completed source indicates a range. The candidate requires re-entry from the volatility band; the middle band is the reference target.");
  if (Number.isFinite(v.rsi)) lines.push(`RSI14 ${v.rsi.toFixed(1)} describes momentum; it is not a separate prediction of direction.`);
  return lines;
}
function strategyExplanation(a) {
  const candidates = strategyCandidates(a), top = candidates[0], ties = top ? candidates.filter((s) => s.fitRank === top.fitRank) : [], lead = top && ties.length === 1 ? `**Conditional ${top.side}: ${top.patternTitle || top.kind.replaceAll("_", " ")}.**` : "**Wait \u2014 no directional entry is justified yet.**";
  return [
    `## ${a.symbol} \xB7 ${a.interval} strategy`,
    lead,
    a.assessment?.reason || "",
    ...a.request.focusPatterns?.length ? ["### Pattern check", ...a.request.focusPatterns.map((k) => {
      const f = a.frames.find((f2) => f2.role === "setup"), events = f.eligible ? f.chartEvidence?.observations.filter((e) => e.kind === k) || [] : [], name = evidenceTitle({ kind: k, side: 1, status: "observed" });
      return events.length ? `- **${name}:** ${events.map((e) => e.status).join(", ")} in completed source bars. Read the anchors and confirmation conditions below.` : `- **${name}: not confirmed** in this eligible completed window. ${k === "head_shoulders" ? "No qualifying alternating shoulders/head and completed neckline break." : "The recorded rules are not met."} This does not exclude an unfinished formation or one outside the checked range.`;
    })] : [],
    "### What the completed chart shows",
    evidenceExplanation(a),
    ...top ? [`### ${ties.length > 1 ? "One conditional alternative" : "Entry, stop and targets"}`, `- **Entry reference:** ${analystPrice(top.entry)} ${a.quote}
- **Stop loss:** ${analystPrice(top.stop)} ${a.quote}
- **Take profit:** ${top.targets.map(analystPrice).join(" / ")} ${a.quote}
- **Reward / risk:** ${top.grossR.toFixed(2)}R gross${top.netR === null ? "; net R needs cost assumptions" : "; " + top.netR.toFixed(2) + "R net"}.`, "### When to act", top.condition, "### Why these levels", top.stopBasis || strategyRationale(a, top).join(" "), top.targetBasis || "", `**Cancel:** ${top.invalidation}`, ties.length > 1 ? "Both directions have equal support. These prices describe only the " + top.side + " alternative; no direction is preferred." : ""] : [],
    ...!top && a.scenarios.length ? ["### Why stay on hold", a.scenarios.map((s) => `- **${s.patternTitle || s.kind} \xB7 ${s.side}:** ${s.contradictions.join(" ") || "No qualifying reward/risk."}`).join("\n")] : [],
    whatIfExplanation(a),
    "### Coin context",
    contextExplanation(a),
    `Completed source cutoff: ${new Date(a.decisionCutoff).toISOString()}. Pattern rules are not measured win probabilities. No entry or fill is confirmed.`
  ].filter(Boolean).join("\n\n");
}
function evidenceDetails(e, a) {
  const t = (x) => new Date(x).toISOString().slice(0, 16) + " UTC", p4 = analystPrice;
  if (e.kind === "rsi_divergence") return `${e.side > 0 ? "Price made a lower low while RSI made a higher low" : "Price made a higher high while RSI made a lower high"}: ${p4(e.priceA)} \u2192 ${p4(e.priceB)}; RSI ${e.oscillatorA.toFixed(1)} \u2192 ${e.oscillatorB.toFixed(1)}, at ${t(e.first)} / ${t(e.last)}. Momentum warning; a price break and retest are still required.`;
  if (["failed_breakout", "trendline_reclaim"].includes(e.kind)) return `A completed close crossed ${e.kind === "trendline_reclaim" ? "a previously confirmed sloping line" : "a previously confirmed pivot level"}, then reclaimed it. ${e.kind === "trendline_reclaim" ? "The line\u2019s reference at this cutoff is" : "The fixed pivot level is"} ${p4(e.priceA)}. A wick-only sweep is treated separately. A new signal-candle break and retest are required.`;
  if (["double_reversal", "head_shoulders", "triple_reversal"].includes(e.kind)) return `${e.touches} confirmed alternating reversal turns after a prior move. Neckline reference ${p4(e.upper)} ${a.quote}; the latest completed close broke it. The geometric height ${p4(e.lower)} gives a projected objective, not guaranteed follow-through. A separate fresh signal break and retest are required for the plan.`;
  if (e.kind === "macd_cross") return `MACD(12,26,9) ${p4(e.oscillatorA)} crossed ${e.side > 0 ? "above" : "below"} its signal ${p4(e.oscillatorB)} on the last completed bar. Momentum context only; no standalone trade is recommended.`;
  if (e.kind === "macd_divergence") return `Price ${p4(e.priceA)} \u2192 ${p4(e.priceB)}, while MACD at the same confirmed price pivots moved ${p4(e.oscillatorA)} \u2192 ${p4(e.oscillatorB)}. Momentum warning; it is correlated with RSI and price moving averages.`;
  if (e.kind === "band_reentry") return `A completed close returned inside Bollinger(20,2) after closing outside. Band edge ${p4(e.priceA)}, middle band ${p4(e.priceB)} ${a.quote}. A range filter is required; a band touch alone is not reversal evidence.`;
  if (e.kind === "ema_pullback") return `An active protected trend tested EMA20 ${p4(e.priceA)} and closed away, preserving confirmed swing ${p4(e.priceB)} ${a.quote}. Further price confirmation is required.`;
  if (e.kind === "candle_rejection") return `A directional engulfing or long-wick candle rejected confirmed price pivot ${p4(e.priceA)} ${a.quote}. The pivot must predate the signal candle; the candle alone does not establish a trend reversal.`;
  if (e.kind === "volatility_squeeze") return `Bollinger bandwidth ${p4(e.priceA)} is below 75% of its prior-window median ${p4(e.priceB)}. Contraction describes volatility; it does not choose breakout direction or create a standalone entry.`;
  if (e.kind === "zone_rejection") return `${e.touches} separated price-pivot tests at ${p4(e.priceA)}\u2013${p4(e.priceB)} ${a.quote}, followed by a directional close away. This is a price reaction zone; volume-at-price and resting orders are unknown.`;
  return `${e.touches} confirmed boundary anchors. Lower ${p4(e.lower)} / upper ${p4(e.upper)} ${a.quote} at the cutoff. ${e.status === "forming" ? "Still forming; neither a breakout nor its direction is confirmed." : e.status === "rejection" ? "The last completed bar tested a boundary and closed away; follow-through is still required." : "A completed boundary break was observed; confirmation needs a fresh signal break and retest."}`;
}
function evidenceExplanation(a) {
  const f = a.frames.find((f2) => f2.role === "setup"), obs = f?.eligible ? f.chartEvidence?.observations || [] : [];
  return [transitionEvidence(f) ? `- **${transitionEvidence(f).title}:** ${transitionEvidence(f).detail}` : f?.structure ? `- **Structure:** ${f.structure.primary} ${f.structure.state}${f.structure.direction ? " \xB7 " + f.structure.direction : ""}.` : "", ...levelNotes(a, analystPrice).map((s) => "- **Source level:** " + s), ...obs.slice(0, 6).map((e) => `- **${evidenceTitle(e)} \xB7 ${e.status}:** ${evidenceDetails(e, a)}`), ...!obs.length ? ["- No current qualifying pattern was detected in the eligible completed window. This is not a claim that no pattern exists outside it."] : [], "- Volume-at-price, order-book supply and news causality are not inferred from OHLC candles."].filter(Boolean).join("\n");
}
function contextExplanation(a) {
  const c = a.assetContext;
  if (!c || c.schema !== "ticking.asset-context.v2") return "Refresh analysis for a recent-issue review.";
  const counts = ["positive", "negative", "uncertain"].map((k) => `${c.issues.filter((i) => i.tone === k).length} ${k === "uncertain" ? "mixed / unconfirmed" : k}`);
  return `Recent issues: ${counts.join(" \xB7 ")}. Open the source cards below for the event, implications and verification gaps. This context does not change native strategy ranking.`;
}

// evidence/arkham-ta-20261020/analyst-engine.mjs
import readline from "node:readline";

// .releases/ticking-arkham-ta-20261020-01/.inputs/src/indicator-knowledge.js
var KNOWLEDGE_VERSION = "analyst-knowledge-v1";
var groups2 = {
  trend: ["sma", "ema", "wma", "rma", "hma", "dema", "tema", "zlema", "linreg", "alma", "ichimoku", "supertrend", "psar", "tickregime"],
  momentum: ["rsi", "macd", "ppo", "stochastic", "stochrsi", "williams", "cci", "roc", "momentum", "ao", "ac", "trix", "kst", "cmo", "dpo", "tsi", "ultimate", "aroon", "bullbear", "rvi", "fisher", "tickmomentum"],
  volatility: ["bb", "bbpct", "bbwidth", "keltner", "envelope", "atr", "atrpct", "stddev", "hv", "chaikinvol", "chop", "mass", "tickenvelope"],
  participation: ["vwma", "vwap", "mfi", "obv", "adl", "cmf", "force", "chaikin", "volosc", "eom", "tickvolume"],
  structure: ["donchian", "pivots"],
  custom: ["expr", "pine"]
};
var specific = {
  rsi: ["Wilder-smoothed gains versus losses.", "Above 70 describes strong recent momentum; reversal requires price-structure evidence. Divergence requires two confirmed price and oscillator swings."],
  cci: ["Typical-price deviation from its moving mean, scaled by mean absolute deviation.", "An extreme is a relative deviation, not a probability or a standalone reversal signal."],
  macd: ["Fast EMA minus slow EMA, its signal EMA and their difference.", "A histogram change describes momentum acceleration/deceleration, not a confirmed trend reversal."],
  bb: ["SMA with population-standard-deviation bands.", "A band touch is location evidence. Trend continuation and range reversion require different conditions."],
  bbpct: ["Position within the Bollinger envelope.", "Values above 1 or below 0 indicate prices outside the envelope; they are not calibrated probabilities."],
  bbwidth: ["Band width relative to its SMA, expressed as percent.", "Compression is low relative dispersion, not a forecast of breakout direction."],
  atr: ["Wilder RMA of true range, including previous-close gaps.", "Non-directional price units. Compare with target distance and costs; never use ATR alone to choose long or short."],
  atrpct: ["Wilder ATR divided by close, in percent.", "Normalizes price scale; liquidity, gaps and contract risks still differ across instruments."],
  adx: ["Wilder-smoothed directional movement strength.", "Strength is separate from direction. Read +DI/-DI and confirmed structure; a threshold is a configurable rule."],
  dmi: ["Positive/negative directional movement with ADX.", "Directional movement is derived from OHLC; it is not buyer/seller order-flow attribution."],
  vwap: ["UTC-session cumulative typical price weighted by recorded volume.", "The complete UTC-session prefix is required. A mid-session window, missing interval or unavailable volume makes decision output unavailable."],
  obv: ["Cumulative recorded volume signed by close-to-close direction.", "This is not classified aggressive buying/selling; a changed history prefix changes the cumulative baseline."],
  mfi: ["Typical-price-direction weighted recorded money flow.", "It is an OHLCV proxy; do not describe it as verified institutional capital movement."],
  pivots: ["Levels derived from the previous complete UTC day.", "The prior session must be complete; the current day cannot provide its own final pivot levels."],
  ichimoku: ["High/low midpoint conversion/base series and displaced leading spans.", "A visual displacement is not future information. Respect the source and confirmation timestamp."],
  donchian: ["Rolling completed high/low envelope.", "A breakout comparison excludes the current bar from the comparison channel."],
  supertrend: ["ATR-offset trailing price boundary.", "A historical boundary or direction change is not a guaranteed executable stop."],
  psar: ["Iterative accelerating trailing boundary.", "It is sensitive to trend changes and range whipsaws; no fill or liquidation behavior is modeled."],
  hv: ["Log-return standard deviation annualized using 365 UTC days.", "An annualization convention is not a future volatility forecast."],
  stochastic: ["Close location within its rolling high/low range, then smoothing.", "Repeated extreme readings can persist during trends; do not count StochRSI and RSI as independent evidence."],
  stochrsi: ["Stochastic normalization of RSI, then smoothing.", "It is a transformation of RSI, not an independent momentum confirmation."],
  chop: ["Log ratio of summed true range to rolling price range.", "A regime descriptor; it does not choose a profitable direction."],
  tickregime: ["Bounded composite of EMA spread normalized by ATR and RSI.", "Shares inputs with EMA/ATR/RSI; avoid counting these as four independent votes."],
  tickmomentum: ["Bounded RSI plus normalized EMA-distance composite.", "Price-derived momentum pressure, not a measurement of aggressive order flow."],
  tickvolume: ["Recorded volume relative to its moving mean.", "Window- and source-specific participation, never a complete global execution census."],
  tickenvelope: ["EMA plus/minus a configured Wilder ATR multiple.", "A volatility envelope with explicit parameters; a touch alone is not an entry condition."],
  expr: ["Bounded caller-authored formula over available inputs.", "Meaning comes from the supplied expression and dependencies; there is no default strategy interpretation."],
  pine: ["Independently implemented, bounded Pine-style subset.", "Supported syntax and inputs only. Plotting a signal does not establish strategy performance."]
};
groups2.trend.push("adx", "dmi");
var generic = { trend: ["A price/structure trend descriptor.", "Lag and whipsaws depend on its smoothing and parameters. Require confirmed structure and timeframe context."], momentum: ["A transformation of recent price momentum.", "Related oscillators share information. Extreme values do not on their own establish reversal or direction."], volatility: ["A price-range or dispersion descriptor.", "Volatility is separate from direction; compare changes within the same source and timeframe."], participation: ["An OHLCV-derived participation descriptor.", "Requires recorded volume and its source definition; it does not identify traders or beneficial ownership."], structure: ["A price-level descriptor from completed source bars.", "Levels are hypotheses with explicit lookback and invalidation, not guaranteed support or resistance."], custom: ["A bounded user-specified calculation.", "Validate every dependency and document the formula before interpreting it."] };
function studyKnowledge(id) {
  const family = Object.keys(groups2).find((k) => groups2[k].includes(id));
  if (!family) throw Error("No reviewed knowledge entry for " + id);
  const [definition, caution] = specific[id] || generic[family];
  return { id, version: KNOWLEDGE_VERSION, family, definition, caution, calculation: "indicator-math.js:" + id, inputs: family === "participation" ? "recorded_OHLCV" : "completed_source_prices", interpretation: "conditional_evidence", independentVote: false };
}
function explainStudy(study, regime2) {
  const k = studyKnowledge(study.family || study.id), values = Object.entries(study.values || {}).map(([n, v]) => n + ": " + (Number.isFinite(v) ? Number(v.toPrecision(7)) : "unavailable")).join(" \xB7 ");
  return `${k.id.toUpperCase()} \xB7 ${k.family}
${k.definition}
${values || "No current output."}
${study.eligibleForAnalysis === false ? "Decision use is unavailable: " + (study.unavailableReason || "source quality restriction") + ". " : ""}Context: ${regime2 || "not classified"}. ${k.caution}`;
}

// evidence/arkham-ta-20261020/analyst-engine.mjs
var MAX = 18e5;
var cache2 = /* @__PURE__ */ new Map();
for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let result;
  try {
    if (Buffer.byteLength(line) > MAX) throw Error("Analyst input exceeds the bounded protocol.");
    const input = JSON.parse(line);
    if (!Array.isArray(input.frames) || input.frames.length < 1 || input.frames.length > 4 || !Number.isSafeInteger(input.cutoff) || !input.request) throw Error("Invalid analyst input.");
    const features = input.frames.map((frame2, i) => {
      const key = frameRevision(frame2) + ":" + Math.floor(input.cutoff / 1e3);
      let f = cache2.get(key);
      if (!f) {
        f = frameFeatures(frame2, input.cutoff);
        cache2.set(key, f);
        while (cache2.size > 24) cache2.delete(cache2.keys().next().value);
      }
      const native = input.native?.[i];
      if (!native || native.origin !== "economic_machine_cpp20" || native.bars !== f.bars) throw Error("Native analysis source mismatch.");
      for (const [name, value] of Object.entries(f.values)) {
        const n = native.values[name];
        if (value === n || Number.isFinite(value) && Number.isFinite(n) && Math.abs(value - n) <= 1e-10 * Math.max(1e-12, Math.abs(value), Math.abs(n))) continue;
        throw Error("Native calculation conformance failed: " + name);
      }
      if (!evidenceConforms(native.chartEvidence, f.chartEvidence)) throw Error("Native chart evidence conformance failed.");
      if (!structureConforms(native.structure, f.structure)) throw Error("Native structure regime conformance failed.");
      for (const name of ["latestHigh", "latestLow"]) {
        const a = native.structure[name], b = f.structure[name];
        if (!a && !b) continue;
        if (!a || !b || a.time !== b.time || a.price !== b.price) throw Error("Native confirmed swing conformance failed: " + name);
      }
      return { ...f, values: native.values, chartEvidence: native.chartEvidence, structure: { ...f.structure, ...native.structure }, origin: native.origin, kernelParityVerified: true };
    });
    const analysis = analyzeFrames({ ...input, features });
    analysis.summary = analysis.ruleVersion === "source-analyst-v3" ? strategyExplanation(analysis) : analysisSummary(analysis);
    const frame = input.frames.find((f) => f.interval === input.request.interval), focus = (input.request.focusStudies || []).map((id) => {
      const params = input.request.focusParameters?.[id] || {}, def = indicatorCatalog().find((d2) => d2.id === id);
      if (!def || Object.keys(params).some((k) => !def.parameters.some((p4) => p4.id === k))) throw Error("Unsupported study parameter.");
      return { id, ...params };
    });
    const studies = focus.length ? analyzeMarket(frame, { now: input.cutoff, maxAgeMs: Math.min(864e5, Math.max(12e4, features.find((f) => f.interval === frame.interval).period * 1.1)), studies: focus }).studies : features.find((f) => f.interval === input.request.interval).studies;
    analysis.studyInterpretations = (input.request.focusStudies || []).slice(0, 5).map((id) => {
      const s = studies.find((s2) => s2.family === id);
      return { knowledge: studyKnowledge(id), summary: s ? explainStudy(s, analysis.regime) : "Add this study to evaluate its configured values; " + studyKnowledge(id).caution };
    });
    if (input.request.mode === "explain" && analysis.studyInterpretations.length) analysis.summary = analysis.studyInterpretations.map((x) => x.summary).join("\n\n") + "\n\n" + analysis.summary;
    if (input.request.mode === "opposite") {
      const side = input.request.referenceSide === "long" ? "short" : "long";
      analysis.summary = "Opposite scenario: " + side + ". " + (analysis.scenarios.find((s) => s.side === side)?.condition || "No eligible opposite setup.") + "\n\n" + analysis.summary;
    }
    for (const d2 of indicatorCatalog()) studyKnowledge(d2.id);
    result = { ok: true, analysis };
  } catch (e) {
    result = { ok: false, error: String(e.message).slice(0, 300) };
  }
  const text = JSON.stringify(result);
  process.stdout.write((Buffer.byteLength(text) <= MAX ? text : JSON.stringify({ ok: false, error: "Analyst response exceeds the byte bound." })) + "\n");
}
