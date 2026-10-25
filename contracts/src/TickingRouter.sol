// SPDX-License-Identifier: MIT
// !!! UNAUDITED. DO NOT DEPLOY WITH REAL FUNDS BEFORE AN INDEPENDENT AUDIT !!!
pragma solidity 0.8.26;

/// @title TickingRouter v1
/// @author Ticking
/// @notice Ownerless, immutable, stateless executor for routes produced by the Ticking off-chain engine on
///         Robinhood Chain. A route is a tightly bit-packed program (see ARCHITECTURE.md §Encoding) interpreted
///         by a single loop. No ABI structs on the hot path, no persistent storage, no admin, no upgrade.
/// @dev    Global invariants (machine-checked in test/, see INVARIANTS.md):
///         I1 Payer is always msg.sender. No input lets the router move anyone else's tokens.
///         I2 Router balance and allowance of every touched token is identical before and after a call.
///         I3 Recipient's tokenOut delta >= minOut, otherwise the whole call reverts.
///         I4 Callbacks pay only an authenticated pool (factory-verified or immutable singleton), only once,
///            only the exact amount the program specified.
///         I5 Token identity: first input is tokenIn, every hop input is the previous hop output, every
///            terminal output is tokenOut. No hop may silently swap identity.
///         I6 Exact-in is exact: every hop consumes exactly its amount or the call reverts (quote == fill).
contract TickingRouter {
    // ───────────────────────────── opcodes (low nibble of op byte) ─────────────────────────────
    uint256 internal constant OP_V3 = 1; // V3 fork, factory.getPool(t0,t1,uint24 fee)
    uint256 internal constant OP_V3TS = 2; // V3 fork, factory.getPool(t0,t1,int24 tickSpacing)
    uint256 internal constant OP_ALG = 3; // Algebra, factory.poolByPair(t0,t1)
    uint256 internal constant OP_V4 = 4; // Uniswap V4 PoolManager
    uint256 internal constant OP_WOO = 5; // WooPP (immutable)
    uint256 internal constant OP_FERMI = 6; // Fermi PMM pool (untrusted address, delta-measured)
    uint256 internal constant OP_TESS = 7; // Tessera (immutable, delta-measured)
    uint256 internal constant OP_BAL = 8; // Balancer V3 vault (immutable, delta-measured)
    uint256 internal constant OP_KYB = 9; // Kyber MetaAggregationRouterV2 (immutable, sandboxed, delta-measured)
    uint256 internal constant OP_PCS = 10; // Pancake Infinity CL (vault.lock -> lockAcquired)
    /// @dev V4 hook address flags BEFORE_SWAP_RETURNS_DELTA (1<<3) | AFTER_SWAP_RETURNS_DELTA (1<<2): amount-altering.
    uint256 internal constant V4_DELTA_HOOKS = 0x0c;
    /// @dev Pancake Infinity `parameters` bitmap offsets 10 (beforeSwapReturnDelta) and 11 (afterSwapReturnDelta).
    uint256 internal constant PCS_DELTA_HOOKS = 0x0c00;
    uint256 internal constant F_CHAIN = 0x80; // input = previous hop output held by router
    uint256 internal constant F_TERM = 0x40; // output goes to recipient and must be tokenOut

    uint160 internal constant MIN_SQRT_P1 = 4295128740;
    uint160 internal constant MAX_SQRT_M1 = 1461446703485210103287273052203988822378723970341;

    // ───────────────────────────── transient slots (EIP-1153) ─────────────────────────────
    uint256 internal constant T_LOCK = 0x7469636b2e6c6f636b; // "tick.lock"
    uint256 internal constant T_POOL = 0x7469636b2e706f6f6c; // expected callback sender, single use
    uint256 internal constant T_TIN = 0x7469636b2e74696e; // token owed in callback
    uint256 internal constant T_AMT = 0x7469636b2e616d74; // exact amount owed
    uint256 internal constant T_SRC = 0x7469636b2e737263; // payer (non-zero) or 0 = router-held funds
    uint256 internal constant T_ZFO = 0x7469636b2e7a666f; // 1 if owed side is token0

    // ───────────────────────────── immutables ─────────────────────────────
    address public immutable POOL_MANAGER;
    address public immutable BAL_VAULT;
    address public immutable WOO;
    address public immutable TESSERA;
    address public immutable KYBER;
    address public immutable F_UNI; // fid 0 (fee)
    address public immutable F_SUSHI; // fid 1 (fee)
    address public immutable F_FORK; // fid 2 (fee)
    address public immutable F_TS0; // fid 0 (tickSpacing)
    address public immutable F_TS1; // fid 1 (tickSpacing)
    address public immutable F_ALG; // Algebra factory
    address public immutable PCS_VAULT;
    address public immutable PCS_CLPM;

    // ───────────────────────────── errors / events ─────────────────────────────
    error Reentrancy();
    error Expired();
    error Malformed();
    error BadToken();
    error BadPool();
    error BadCallback();
    error PartialFill();
    error Slippage(uint256 out, uint256 minOut);
    error TransferFailed();

    /// @notice One event per executed route. `amountIn` is the exact total pulled from `payer`.
    event Swap(
        address indexed payer,
        address indexed recipient,
        address indexed tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOut
    );

    /// @dev All venue addresses are fixed forever. Adding a venue = deploying a new router.
    constructor(address[13] memory a) {
        for (uint256 i; i < 13; ++i) {
            if (a[i] == address(0)) revert Malformed();
        }
        (POOL_MANAGER, BAL_VAULT, WOO, TESSERA, KYBER) = (a[0], a[1], a[2], a[3], a[4]);
        (F_UNI, F_SUSHI, F_FORK, F_TS0, F_TS1, F_ALG) = (a[5], a[6], a[7], a[8], a[9], a[10]);
        (PCS_VAULT, PCS_CLPM) = (a[11], a[12]);
    }

    // ════════════════════════════════════ entry ════════════════════════════════════

    /// @notice Execute a packed route program. The only state-changing entry point for users.
    /// @param p Program: header(tokenIn 20 | tokenOut 20 | recipient 20 | minOut 16 | deadline 4) then ops.
    /// @return out Amount of tokenOut credited to recipient (balance delta, fee-on-transfer safe).
    function swap(bytes calldata p) external returns (uint256 out) {
        assembly ("memory-safe") {
            // I-lock: tload(T_LOCK) != 0 means we are inside a swap; any re-entry reverts.
            if tload(T_LOCK) {
                mstore(0, 0xab143c06) // Reentrancy()
                revert(0x1c, 4)
            }
            tstore(T_LOCK, 1)
        }
        address tokenIn = address(uint160(_rd(p, 0, 20)));
        address tokenOut = address(uint160(_rd(p, 20, 20)));
        address recipient = address(uint160(_rd(p, 40, 20)));
        uint256 minOut = _rd(p, 60, 16);
        if (block.timestamp > _rd(p, 76, 4)) revert Expired();
        if (
            tokenIn == tokenOut || tokenIn == address(0) || tokenOut == address(0) || recipient == address(0)
                || recipient == address(this)
        ) revert BadToken();

        uint256 rcptBefore = _bal(tokenOut, recipient);
        uint256 total;
        uint256 last; // output of previous non-terminal hop, held by router
        address prevOut; // its token
        uint256 i = 80;
        while (i < p.length) {
            uint256 op = _rd(p, i, 1);
            unchecked {
                ++i;
            } // i < p.length <= calldatasize, cannot overflow
            bool chain = op & F_CHAIN != 0;
            bool term = op & F_TERM != 0;
            // I5 state machine: a pending router-held output must be consumed by the very next hop, and only then.
            if (chain != (prevOut != address(0))) revert Malformed();
            uint256 a;
            address tin;
            if (chain) {
                (a, tin) = (last, prevOut);
            } else {
                a = _rd(p, i, 16);
                i += 16;
                tin = tokenIn;
                total += a;
            }
            address tout = address(uint160(_rd(p, i, 20)));
            i += 20;
            if (a == 0 || tout == tin || tout == address(0)) revert Malformed();
            if (term ? tout != tokenOut : tout == tokenOut) revert BadToken();
            address to = term ? recipient : address(this);
            address src = chain ? address(0) : msg.sender;
            uint256 got;
            (got, i) = _hop(p, i, op & 0x0f, tin, tout, a, src, to);
            if (term) {
                prevOut = address(0);
                last = 0;
            } else {
                if (got == 0) revert PartialFill();
                (prevOut, last) = (tout, got);
            }
        }
        if (prevOut != address(0) || total == 0) revert Malformed(); // program must end on a terminal hop
        out = _bal(tokenOut, recipient) - rcptBefore;
        if (out < minOut) revert Slippage(out, minOut);
        // Justification: whole call is under the transient lock; event order is irrelevant
        // slither-disable-next-line reentrancy-events
        emit Swap(msg.sender, recipient, tokenIn, tokenOut, total, out);
        assembly ("memory-safe") {
            tstore(T_LOCK, 0)
        }
    }

    // ════════════════════════════════════ interpreter ════════════════════════════════════

    /// @dev Executes one hop. `src` = payer for JIT pulls, or 0 when the router holds the input.
    ///      Returns output credited to `to` and the new cursor.
    function _hop(bytes calldata p, uint256 i, uint256 k, address tin, address tout, uint256 a, address src, address to)
        internal
        returns (uint256 got, uint256 j)
    {
        bool zfo = tin < tout;
        (address t0, address t1) = zfo ? (tin, tout) : (tout, tin);
        if (k <= OP_ALG) {
            address pool;
            if (k == OP_ALG) {
                pool = address(uint160(_rd(p, i, 20)));
                j = i + 20;
                if (IAlgFactory(F_ALG).poolByPair(t0, t1) != pool) revert BadPool();
            } else {
                uint256 fid = _rd(p, i, 1);
                uint256 param = _rd(p, i + 1, 3);
                pool = address(uint160(_rd(p, i + 4, 20)));
                j = i + 24;
                address f;
                if (k == OP_V3) {
                    f = fid == 0 ? F_UNI : fid == 1 ? F_SUSHI : fid == 2 ? F_FORK : address(0);
                    if (f == address(0) || IV3Factory(f).getPool(t0, t1, uint24(param)) != pool) revert BadPool();
                } else if (k == OP_V3TS) {
                    f = fid == 0 ? F_TS0 : fid == 1 ? F_TS1 : address(0);
                    if (f == address(0) || IV3TSFactory(f).getPool(t0, t1, int24(uint24(param))) != pool) {
                        revert BadPool();
                    }
                } else {
                    revert Malformed(); // k == 0
                }
            }
            if (pool == address(0)) revert BadPool();
            _arm(pool, tin, a, src, zfo);
            (int256 d0, int256 d1) = IV3Pool(pool).swap(to, zfo, int256(a), zfo ? MIN_SQRT_P1 : MAX_SQRT_M1, "");
            _disarmed();
            int256 o = zfo ? d1 : d0;
            if (o >= 0) revert PartialFill();
            got = uint256(-o); // pool is factory-verified: its return value is trusted
        } else if (k == OP_V4) {
            uint256 fee = _rd(p, i, 3);
            uint256 ts = _rd(p, i + 3, 3);
            address hooks = address(uint160(_rd(p, i + 6, 20)));
            j = i + 26;
            // Hooks that return deltas can change amounts vs the quote: rejected. Others (e.g. dynamic fee) are
            // allowed; the exact-in check in the callback is the second line of defense.
            if (uint160(hooks) & V4_DELTA_HOOKS != 0) revert BadPool();
            _arm(POOL_MANAGER, tin, a, src, zfo);
            uint256 b = _bal(tout, to);
            // Justification: output is measured by balance delta at `to`
            // slither-disable-next-line unused-return
            IPoolManager(POOL_MANAGER).unlock(abi.encode(PoolKey(t0, t1, uint24(fee), int24(uint24(ts)), hooks), to));
            _disarmed();
            got = _bal(tout, to) - b;
        } else if (k == OP_PCS) {
            uint256 fee = _rd(p, i, 3);
            bytes32 params = bytes32(_rd(p, i + 3, 32));
            address hooks = address(uint160(_rd(p, i + 35, 20)));
            j = i + 55;
            if (hooks != address(0) && uint256(params) & PCS_DELTA_HOOKS != 0) revert BadPool();
            _arm(PCS_VAULT, tin, a, src, zfo);
            uint256 b = _bal(tout, to);
            // Justification: output is measured by balance delta at `to`
            // slither-disable-next-line unused-return
            IPcsVault(PCS_VAULT).lock(abi.encode(PcsKey(t0, t1, hooks, PCS_CLPM, uint24(fee), params), to));
            _disarmed();
            got = _bal(tout, to) - b;
        } else {
            // Kyber output is always measured at the router (its calldata names the receiver; we never trust it),
            // then forwarded. Every other venue is measured where it delivers.
            address m = k == OP_KYB ? address(this) : to;
            uint256 b = _bal(tout, m);
            if (k == OP_WOO) {
                j = i;
                _pay(tin, src, WOO, a);
                // Justification: output is measured by balance delta, never trusted
                // slither-disable-next-line unused-return
                IWooPP(WOO).swap(tin, tout, a, 0, to, address(0));
            } else if (k == OP_BAL) {
                address pool = address(uint160(_rd(p, i, 20)));
                j = i + 20;
                _arm(BAL_VAULT, tin, a, src, zfo);
                // Justification: callback result is measured by balance delta
                // slither-disable-next-line unused-return
                IBalVault(BAL_VAULT).unlock(abi.encodeCall(this.balancerCallback, (pool, tout, to)));
                _disarmed();
            } else {
                address target = address(0);
                if (k == OP_FERMI) {
                    target = address(uint160(_rd(p, i, 20)));
                    j = i + 20;
                } else if (k == OP_TESS) {
                    target = TESSERA;
                    j = i;
                } else if (k == OP_KYB) {
                    target = KYBER;
                    uint256 n = _rd(p, i, 2);
                    j = i + 2 + n;
                    if (j > p.length || n < 4) revert Malformed();
                    bytes4 sel = bytes4(p[i + 2:i + 6]);
                    if (sel != 0xe21fd0e9 && sel != 0x8af033fb) revert Malformed();
                } else {
                    revert Malformed();
                }
                if (target == address(0) || target == tin || target == tout) revert BadPool();
                // Approval-based venues need router-held input. `pre` = router tin balance the venue must leave behind.
                uint256 pre = _bal(tin, address(this));
                if (src != address(0)) {
                    _transferFrom(tin, src, address(this), a);
                    if (_bal(tin, address(this)) - pre != a) revert BadToken(); // fee-on-transfer input rejected (I6)
                } else {
                    pre -= a; // chained: router holds `a` of tin from the previous hop
                }
                _approve(tin, target, a);
                if (k == OP_FERMI) {
                    // Justification: untrusted venue: output measured by delta
                    // slither-disable-next-line unused-return
                    IFermi(target).swapExactIn(zfo, a, 0, to, block.timestamp);
                } else if (k == OP_TESS) {
                    ITessera(target).tesseraSwapWithAllowances(tin, tout, int256(a), 0, to, "");
                } else {
                    (bool ok,) = target.call(p[i + 2:j]);
                    if (!ok) revert BadCallback();
                }
                // I6 + I2: venue must have consumed the full allowance; nothing lingers.
                // A venue that hands tin back (e.g. Kyber calldata routing tin -> router) would strand funds: rejected.
                if (IERC20(tin).allowance(address(this), target) != 0 || _bal(tin, address(this)) != pre) {
                    revert PartialFill();
                }
            }
            got = _bal(tout, m) - b;
            if (m != to) _transfer(tout, to, got);
        }
    }

    // ════════════════════════════════════ callbacks ════════════════════════════════════

    /// @notice V3-family swap callbacks: uniswapV3/sushi/fork (0xfa461e33), algebra (0x2c8958f6),
    ///         pancakeV3 (0x23a69e75), ramsesV2 (0x654b6487). Layout (int256 a0, int256 a1, bytes).
    /// @dev    Auth: msg.sender must equal the single-use transient T_POOL armed by _hop for a factory-verified pool.
    fallback() external {
        bytes4 s = msg.sig;
        if (s != 0xfa461e33 && s != 0x2c8958f6 && s != 0x23a69e75 && s != 0x654b6487) revert BadCallback();
        (address tin, uint256 a, address src, bool zfo) = _consume(msg.sender);
        // Short calldata reverts in the bounds-checked slice. The output side is validated after swap() returns.
        (int256 a0, int256 a1) = abi.decode(msg.data[4:68], (int256, int256));
        // I6: the owed side must be exactly `a`.
        if ((zfo ? a0 : a1) != int256(a)) revert PartialFill();
        _pay(tin, src, msg.sender, a);
    }

    /// @notice Uniswap V4 unlock callback. Only the PoolManager, only while armed.
    function unlockCallback(bytes calldata raw) external returns (bytes memory) {
        // tin/a/src/zfo come from transient context armed by _hop, never from calldata.
        (address tin, uint256 a, address src, bool zfo) = _consume(POOL_MANAGER);
        (PoolKey memory key, address to) = abi.decode(raw, (PoolKey, address));
        int256 d =
            IPoolManager(POOL_MANAGER).swap(key, SwapParams(zfo, -int256(a), zfo ? MIN_SQRT_P1 : MAX_SQRT_M1), "");
        _settleTake(POOL_MANAGER, d, tin, a, src, zfo, zfo ? key.c1 : key.c0, to);
        return "";
    }

    /// @notice Pancake Infinity vault lock callback. Only the vault, only while armed.
    function lockAcquired(bytes calldata raw) external returns (bytes memory) {
        (address tin, uint256 a, address src, bool zfo) = _consume(PCS_VAULT);
        (PcsKey memory key, address to) = abi.decode(raw, (PcsKey, address));
        int256 d = IPcsCLPM(PCS_CLPM).swap(key, SwapParams(zfo, -int256(a), zfo ? MIN_SQRT_P1 : MAX_SQRT_M1), "");
        _settleTake(PCS_VAULT, d, tin, a, src, zfo, zfo ? key.c1 : key.c0, to);
        return "";
    }

    /// @dev Shared V4 / Pancake-Infinity flash-accounting settlement. `d` is the caller BalanceDelta
    ///      (amount0 << 128 | amount1). I6: input delta must be exactly -a, output strictly positive.
    function _settleTake(address v, int256 d, address tin, uint256 a, address src, bool zfo, address tout, address to)
        internal
    {
        int128 d0 = int128(d >> 128);
        int128 d1 = int128(d);
        (int128 din, int128 dout) = zfo ? (d0, d1) : (d1, d0);
        if (din != -int128(int256(a)) || dout <= 0) revert PartialFill();
        IPoolManager(v).sync(tin);
        _pay(tin, src, v, a);
        // Justification: settled amount == a is enforced by the din check above
        // slither-disable-next-line unused-return
        IPoolManager(v).settle();
        IPoolManager(v).take(tout, to, uint256(uint128(dout)));
    }

    /// @notice Balancer V3 vault unlock callback (exact-in single swap). Only the vault, only while armed.
    function balancerCallback(address pool, address tout, address to) external returns (bytes memory) {
        (address tin, uint256 a, address src,) = _consume(BAL_VAULT);
        // Justification: exact-in; the vault's own delta accounting reverts unlock() unless exactly `a` is settled
        // slither-disable-next-line unused-return
        (,, uint256 aout) = IBalVault(BAL_VAULT).swap(VaultSwapParams(0, pool, tin, tout, a, 0, ""));
        _pay(tin, src, BAL_VAULT, a);
        // Justification: vault reverts on under-payment; credit == a by construction
        // slither-disable-next-line unused-return
        IBalVault(BAL_VAULT).settle(tin, a);
        IBalVault(BAL_VAULT).sendTo(tout, to, aout);
        return "";
    }

    // ════════════════════════════════════ internals ════════════════════════════════════

    /// @dev Bounds-checked big-endian read of `n` (1..32) bytes at offset `o` of program `p`.
    function _rd(bytes calldata p, uint256 o, uint256 n) internal pure returns (uint256 v) {
        if (o + n > p.length) revert Malformed(); // calldataload past end would silently read zeros
        assembly ("memory-safe") {
            v := shr(sub(256, shl(3, n)), calldataload(add(p.offset, o)))
        }
    }

    function _arm(address pool, address tin, uint256 a, address src, bool zfo) internal {
        assembly ("memory-safe") {
            tstore(T_POOL, pool)
            tstore(T_TIN, tin)
            tstore(T_AMT, a)
            tstore(T_SRC, src)
            tstore(T_ZFO, zfo)
        }
    }

    /// @dev Single-use auth: returns armed context and clears T_POOL so a second callback cannot be paid.
    function _consume(address expected) internal returns (address tin, uint256 a, address src, bool zfo) {
        address pool;
        assembly ("memory-safe") {
            pool := tload(T_POOL)
            tin := tload(T_TIN)
            a := tload(T_AMT)
            src := tload(T_SRC)
            zfo := tload(T_ZFO)
            tstore(T_POOL, 0)
        }
        // expected is a non-zero immutable or a factory-verified pool, so pool == 0 is covered by pool != expected.
        if (pool != expected || msg.sender != expected) revert BadCallback();
    }

    /// @dev After the venue returns, the callback must have fired exactly once (T_POOL consumed).
    function _disarmed() internal view {
        address pool;
        assembly ("memory-safe") {
            pool := tload(T_POOL)
        }
        if (pool != address(0)) revert BadCallback();
    }

    function _pay(address t, address src, address to, uint256 a) internal {
        if (src == address(0)) _transfer(t, to, a);
        else _transferFrom(t, src, to, a);
    }

    function _bal(address t, address who) internal view returns (uint256 v) {
        assembly ("memory-safe") {
            mstore(0x00, 0x70a08231) // balanceOf(address)
            mstore(0x20, who)
            if iszero(and(gt(returndatasize(), 0x1f), staticcall(gas(), t, 0x1c, 0x24, 0x00, 0x20))) {
                // returndatasize is checked after the call (evaluated right-to-left by `and`)
                mstore(0, 0x90b8ec18) // TransferFailed()
                revert(0x1c, 4)
            }
            v := mload(0x00)
        }
    }

    /// @dev Solady-style: success iff call ok AND (no code-return && has code) or returned true.
    function _transfer(address t, address to, uint256 a) internal {
        assembly ("memory-safe") {
            let m := mload(0x40)
            mstore(0x00, 0xa9059cbb)
            mstore(0x20, to)
            mstore(0x40, a)
            let ok := call(gas(), t, 0, 0x1c, 0x44, 0x00, 0x20)
            if iszero(
                and(
                    ok,
                    or(
                        and(eq(mload(0x00), 1), gt(returndatasize(), 0x1f)),
                        and(iszero(returndatasize()), gt(extcodesize(t), 0))
                    )
                )
            ) {
                mstore(0, 0x90b8ec18)
                revert(0x1c, 4)
            }
            mstore(0x40, m)
        }
    }

    function _transferFrom(address t, address from, address to, uint256 a) internal {
        assembly ("memory-safe") {
            let m := mload(0x40)
            let z := mload(0x60)
            mstore(0x00, 0x23b872dd)
            mstore(0x20, from)
            mstore(0x40, to)
            mstore(0x60, a)
            let ok := call(gas(), t, 0, 0x1c, 0x64, 0x00, 0x20)
            if iszero(
                and(
                    ok,
                    or(
                        and(eq(mload(0x00), 1), gt(returndatasize(), 0x1f)),
                        and(iszero(returndatasize()), gt(extcodesize(t), 0))
                    )
                )
            ) {
                mstore(0, 0x90b8ec18)
                revert(0x1c, 4)
            }
            mstore(0x40, m)
            mstore(0x60, z)
        }
    }

    function _approve(address t, address s, uint256 a) internal {
        assembly ("memory-safe") {
            let m := mload(0x40)
            mstore(0x00, 0x095ea7b3)
            mstore(0x20, s)
            mstore(0x40, a)
            let ok := call(gas(), t, 0, 0x1c, 0x44, 0x00, 0x20)
            if iszero(
                and(
                    ok,
                    or(
                        and(eq(mload(0x00), 1), gt(returndatasize(), 0x1f)),
                        and(iszero(returndatasize()), gt(extcodesize(t), 0))
                    )
                )
            ) {
                mstore(0, 0x90b8ec18)
                revert(0x1c, 4)
            }
            mstore(0x40, m)
        }
    }
}

// ───────────────────────────── minimal external interfaces ─────────────────────────────
interface IERC20 {
    function allowance(address, address) external view returns (uint256);
}

interface IV3Pool {
    function swap(address, bool, int256, uint160, bytes calldata) external returns (int256, int256);
}

interface IV3Factory {
    function getPool(address, address, uint24) external view returns (address);
}

interface IV3TSFactory {
    function getPool(address, address, int24) external view returns (address);
}

interface IAlgFactory {
    function poolByPair(address, address) external view returns (address);
}

struct PoolKey {
    address c0;
    address c1;
    uint24 fee;
    int24 ts;
    address hooks;
}

struct SwapParams {
    bool zfo;
    int256 amt;
    uint160 lim;
}

interface IPoolManager {
    function unlock(bytes calldata) external returns (bytes memory);
    function swap(PoolKey memory, SwapParams memory, bytes calldata) external returns (int256);
    function sync(address) external;
    function settle() external payable returns (uint256);
    function take(address, address, uint256) external;
}

interface IWooPP {
    function swap(address, address, uint256, uint256, address, address) external returns (uint256);
}

struct VaultSwapParams {
    uint8 kind;
    address pool;
    address tokenIn;
    address tokenOut;
    uint256 amountGivenRaw;
    uint256 limitRaw;
    bytes userData;
}

interface IBalVault {
    function unlock(bytes calldata) external returns (bytes memory);
    function swap(VaultSwapParams memory) external returns (uint256, uint256, uint256);
    function settle(address, uint256) external returns (uint256);
    function sendTo(address, address, uint256) external;
}

interface IFermi {
    function swapExactIn(bool, uint256, uint256, address, uint256) external returns (uint256);
}

interface ITessera {
    function tesseraSwapWithAllowances(address, address, int256, uint256, address, bytes calldata) external;
}

struct PcsKey {
    address c0;
    address c1;
    address hooks;
    address poolManager;
    uint24 fee;
    bytes32 parameters;
}

interface IPcsVault {
    function lock(bytes calldata) external returns (bytes memory);
}

interface IPcsCLPM {
    function swap(PcsKey memory, SwapParams memory, bytes calldata) external returns (int256);
}
