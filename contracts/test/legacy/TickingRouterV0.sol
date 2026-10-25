// SPDX-License-Identifier: MIT
// !!! UNAUDITED — DO NOT DEPLOY WITH REAL FUNDS BEFORE AN INDEPENDENT AUDIT !!!
pragma solidity 0.8.26;

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
    function approve(address, uint256) external returns (bool);
}

interface IV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function swap(address, bool, int256, uint160, bytes calldata) external returns (int256, int256);
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

/// @title TickingRouter (UNAUDITED)
/// @notice Executes a precomputed multi-venue route. Holds no funds between txs; any dust of tokenIn is returned to the payer,
///         output goes only to `recipient`. No arbitrary calls: only typed venue ops; callbacks are authenticated.
contract TickingRouterV0 {
    enum K {
        V3,
        V4,
        WOO,
        BAL,
        FERMI,
        TESS
    }

    struct Op {
        K kind; // amt==0 -> use previous op output
        address target;
        bool zfo;
        uint256 amt;
        bytes data;
    }
    address public immutable POOL_MANAGER;
    address public immutable BAL_VAULT;
    address public immutable TESSERA;
    uint256 private locked = 1; // set only for the duration of one pool call
    address private expectedCallback;

    error Reentrancy();
    error Expired();
    error Slippage(uint256 out, uint256 minOut);
    error BadCallback();
    error BadOp();
    event Swapped(
        address indexed payer,
        address indexed recipient,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOut
    );

    constructor(address pm, address balVault, address tessera) {
        POOL_MANAGER = pm;
        BAL_VAULT = balVault;
        TESSERA = tessera;
    }
    modifier nonReentrant() {
        if (locked != 1) revert Reentrancy();
        locked = 2;
        _;
        locked = 1;
    }

    function swap(
        Op[] calldata ops,
        address tokenIn,
        uint256 amountIn,
        address tokenOut,
        uint256 minOut,
        address recipient,
        uint256 deadline
    ) external nonReentrant returns (uint256 out) {
        if (block.timestamp > deadline) revert Expired();
        if (tokenIn == tokenOut || recipient == address(0) || recipient == address(this)) revert BadOp();
        uint256 inBefore = IERC20(tokenIn).balanceOf(address(this));
        uint256 outBefore = IERC20(tokenOut).balanceOf(address(this));
        _safeTransferFrom(tokenIn, msg.sender, address(this), amountIn);
        uint256 last;
        uint256 spent;
        for (uint256 i; i < ops.length; i++) {
            Op calldata o = ops[i];
            uint256 a = o.amt == 0 ? last : o.amt;
            if (o.amt != 0) spent += a;
            last = _exec(o, a);
        }
        if (spent > amountIn) revert BadOp();
        out = IERC20(tokenOut).balanceOf(address(this)) - outBefore;
        if (out < minOut) revert Slippage(out, minOut);
        _safeTransfer(tokenOut, recipient, out);
        uint256 dust = IERC20(tokenIn).balanceOf(address(this)) - inBefore; // never strand payer funds
        if (dust > 0) _safeTransfer(tokenIn, msg.sender, dust);
        emit Swapped(msg.sender, recipient, tokenIn, tokenOut, amountIn, out);
    }

    function _exec(Op calldata o, uint256 a) internal returns (uint256) {
        if (o.kind == K.V3) {
            expectedCallback = o.target;
            (int256 d0, int256 d1) = IV3Pool(o.target)
                .swap(
                    address(this),
                    o.zfo,
                    int256(a),
                    o.zfo ? 4295128740 : 1461446703485210103287273052203988822378723970341,
                    ""
                );
            expectedCallback = address(0);
            return uint256(-(o.zfo ? d1 : d0));
        }
        if (o.kind == K.V4) {
            expectedCallback = POOL_MANAGER;
            uint256 r = abi.decode(IPoolManager(POOL_MANAGER).unlock(abi.encode(uint8(1), o.data, o.zfo, a)), (uint256));
            expectedCallback = address(0);
            return r;
        }
        if (o.kind == K.BAL) {
            expectedCallback = BAL_VAULT;
            IBalVault(BAL_VAULT).unlock(abi.encodeCall(this.balancerCallback, (o.data, a)));
            expectedCallback = address(0);
            uint256 r = _lastBal;
            _lastBal = 0;
            return r;
        }
        if (o.kind == K.WOO) {
            (address tin, address tout) = abi.decode(o.data, (address, address));
            _safeTransfer(tin, o.target, a);
            return IWooPP(o.target).swap(tin, tout, a, 0, address(this), address(0));
        }
        if (o.kind == K.FERMI) {
            address tin = abi.decode(o.data, (address));
            _approveExact(tin, o.target, a);
            uint256 r = IFermi(o.target).swapExactIn(o.zfo, a, 0, address(this), block.timestamp);
            _approveExact(tin, o.target, 0);
            return r;
        }
        if (o.kind == K.TESS) {
            (address tin, address tout) = abi.decode(o.data, (address, address));
            _approveExact(tin, TESSERA, a);
            uint256 b0 = IERC20(tout).balanceOf(address(this));
            ITessera(TESSERA).tesseraSwapWithAllowances(tin, tout, int256(a), 0, address(this), "");
            _approveExact(tin, TESSERA, 0);
            return IERC20(tout).balanceOf(address(this)) - b0;
        }
        revert BadOp();
    }
    uint256 private _lastBal;

    /// Balancer V3 vault callback (exact-in single swap)
    function balancerCallback(bytes calldata d, uint256 a) external returns (bytes memory) {
        if (msg.sender != BAL_VAULT || expectedCallback != BAL_VAULT) revert BadCallback();
        (address pool, address tin, address tout) = abi.decode(d, (address, address, address));
        (,, uint256 amountOut) = IBalVault(BAL_VAULT).swap(VaultSwapParams(0, pool, tin, tout, a, 0, ""));
        _safeTransfer(tin, BAL_VAULT, a);
        IBalVault(BAL_VAULT).settle(tin, a);
        IBalVault(BAL_VAULT).sendTo(tout, address(this), amountOut);
        _lastBal = amountOut;
        return "";
    }

    /// Uniswap V4 unlock callback
    function unlockCallback(bytes calldata raw) external returns (bytes memory) {
        if (msg.sender != POOL_MANAGER || expectedCallback != POOL_MANAGER) revert BadCallback();
        (, bytes memory kd, bool zfo, uint256 a) = abi.decode(raw, (uint8, bytes, bool, uint256));
        PoolKey memory k = abi.decode(kd, (PoolKey));
        if (k.c0 == address(0)) revert BadOp(); // native-ETH pools not supported by this router
        int256 d = IPoolManager(msg.sender)
            .swap(
                k, SwapParams(zfo, -int256(a), zfo ? 4295128740 : 1461446703485210103287273052203988822378723970341), ""
            );
        int128 d0 = int128(d >> 128);
        int128 d1 = int128(d);
        (address cin, address cout, uint256 out) =
            zfo ? (k.c0, k.c1, uint256(uint128(d1))) : (k.c1, k.c0, uint256(uint128(d0)));
        IPoolManager(msg.sender).sync(cin);
        _safeTransfer(cin, msg.sender, a);
        IPoolManager(msg.sender).settle();
        IPoolManager(msg.sender).take(cout, address(this), out);
        return abi.encode(out);
    }

    /// Any V3-fork swap callback (uniswapV3SwapCallback / ramsesV2SwapCallback / algebraSwapCallback / ...): authenticated by expectedCallback.
    fallback() external {
        if (msg.sender != expectedCallback || msg.sender == address(0) || msg.data.length < 68) revert BadCallback();
        (int256 a0, int256 a1) = abi.decode(msg.data[4:68], (int256, int256));
        if (a0 > 0) _safeTransfer(IV3Pool(msg.sender).token0(), msg.sender, uint256(a0));
        if (a1 > 0) _safeTransfer(IV3Pool(msg.sender).token1(), msg.sender, uint256(a1));
    }

    function _approveExact(address t, address s, uint256 v) internal {
        (bool ok, bytes memory r) = t.call(abi.encodeCall(IERC20.approve, (s, v)));
        require(ok && (r.length == 0 || abi.decode(r, (bool))), "approve");
    }

    function _safeTransfer(address t, address to, uint256 v) internal {
        (bool ok, bytes memory r) = t.call(abi.encodeCall(IERC20.transfer, (to, v)));
        require(ok && (r.length == 0 || abi.decode(r, (bool))), "transfer");
    }

    function _safeTransferFrom(address t, address f, address to, uint256 v) internal {
        (bool ok, bytes memory r) = t.call(abi.encodeCall(IERC20.transferFrom, (f, to, v)));
        require(ok && (r.length == 0 || abi.decode(r, (bool))), "transferFrom");
    }
}
