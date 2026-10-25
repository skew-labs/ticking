// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Configurable adversarial ERC20 for negative tests.
contract MockToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public blacklisted;
    uint256 public feeBps; // fee-on-transfer
    bool public returnFalse;
    bool public paused;
    address public reenterTarget;
    bytes public reenterData;

    function mint(address to, uint256 a) external {
        balanceOf[to] += a;
    }

    function setFee(uint256 b) external {
        feeBps = b;
    }

    function setReturnFalse(bool v) external {
        returnFalse = v;
    }

    function setPaused(bool v) external {
        paused = v;
    }

    function setBlacklist(address a, bool v) external {
        blacklisted[a] = v;
    }

    function setReenter(address t, bytes calldata d) external {
        reenterTarget = t;
        reenterData = d;
    }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        return _move(msg.sender, to, a);
    }

    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        uint256 al = allowance[f][msg.sender];
        require(al >= a, "allow");
        if (al != type(uint256).max) allowance[f][msg.sender] = al - a;
        return _move(f, to, a);
    }

    function _move(address f, address to, uint256 a) internal returns (bool) {
        require(!paused, "paused");
        require(!blacklisted[f] && !blacklisted[to], "blacklisted");
        if (returnFalse) return false;
        require(balanceOf[f] >= a, "bal");
        balanceOf[f] -= a;
        uint256 fee = a * feeBps / 10_000;
        balanceOf[to] += a - fee;
        if (reenterTarget != address(0)) {
            (bool ok, bytes memory r) = reenterTarget.call(reenterData);
            if (!ok) {
                assembly {
                    revert(add(r, 32), mload(r))
                }
            }
        }
        return true;
    }
}

contract MockV3Factory {
    mapping(bytes32 => address) public pools;

    function set(address t0, address t1, uint24 fee, address p) external {
        pools[keccak256(abi.encode(t0, t1, fee))] = p;
    }

    function getPool(address t0, address t1, uint24 fee) external view returns (address) {
        return pools[keccak256(abi.encode(t0, t1, fee))];
    }

    function getPool(address t0, address t1, int24 ts) external view returns (address) {
        return pools[keccak256(abi.encode(t0, t1, uint24(ts)))];
    }

    function poolByPair(address t0, address t1) external view returns (address) {
        return pools[keccak256(abi.encode(t0, t1, uint24(0)))];
    }
}

/// @notice Constant-price V3-like pool (price 1:1 minus `skimBps`). `mode` selects adversarial behavior.
contract MockV3Pool {
    address public token0;
    address public token1;
    uint256 public mode; // 0 honest, 1 ask more, 2 callback twice, 3 no callback, 4 partial fill, 5 report fake out
    bytes4 public cbSel = 0xfa461e33;

    constructor(address a, address b) {
        (token0, token1) = a < b ? (a, b) : (b, a);
    }

    function setMode(uint256 m) external {
        mode = m;
    }

    address public poker;

    function setPoker(address p) external {
        poker = p;
    }

    function setSel(bytes4 s) external {
        cbSel = s;
    }

    function swap(address to, bool zfo, int256 amt, uint160 lim, bytes calldata)
        external
        returns (int256 d0, int256 d1)
    {
        // a real V3 pool reverts (SPL) if the limit is on the wrong side; mirror that exactly
        require(zfo ? lim == 4295128740 : lim == 1461446703485210103287273052203988822378723970341, "SPL");
        uint256 a = uint256(amt);
        uint256 pay = mode == 4 ? a / 2 : a;
        (address tin, address tout) = zfo ? (token0, token1) : (token1, token0);
        uint256 out = pay * 997 / 1000;
        if (mode == 5) out = 1;
        if (mode == 6) out = 0;
        MockToken(tout).transfer(to, out);
        int256 owed = int256(mode == 1 ? pay + 1 : pay);
        int256 rep = mode == 5 ? -int256(out * 1000) : mode == 7 ? int256(1) : -int256(out);
        (d0, d1) = zfo ? (owed, rep) : (rep, owed);
        uint256 before = MockToken(tin).balanceOf(address(this));
        if (mode == 8) MockSingleton(poker).poke(msg.sender);
        if (mode == 9) {
            EvilCaller(poker).hitUnlock(msg.sender);
        }
        if (mode != 3) {
            (bool ok, bytes memory r) = msg.sender.call(abi.encodeWithSelector(cbSel, d0, d1, ""));
            if (!ok) {
                assembly {
                    revert(add(r, 32), mload(r))
                }
            }
        }
        if (mode == 2) {
            (bool ok, bytes memory r) = msg.sender.call(abi.encodeWithSelector(cbSel, d0, d1, ""));
            if (!ok) {
                assembly {
                    revert(add(r, 32), mload(r))
                }
            }
        }
        require(MockToken(tin).balanceOf(address(this)) >= before + (mode == 3 ? 0 : uint256(owed)), "IIA");
    }
}

/// @notice Attacker pool: calls the router callback directly to try to get paid without being armed.
contract EvilCaller {
    function hitUnlock(address router) external {
        (bool ok, bytes memory e) = router.call(abi.encodeWithSignature("unlockCallback(bytes)", hex""));
        if (!ok) {
            assembly {
                revert(add(e, 32), mload(e))
            }
        }
    }

    function hit(address router, int256 a0, int256 a1) external returns (bool ok) {
        (ok,) = router.call(abi.encodeWithSelector(bytes4(0xfa461e33), a0, a1, ""));
    }
}

/// @notice Approval-based venue mock (Fermi-like). mode 0 honest, 1 pulls less than approved, 2 pulls from victim.
contract MockFermi {
    address public tin;
    address public tout;
    uint256 public mode;
    address public victim;

    constructor(address a, address b) {
        (tin, tout) = (a, b);
    }

    function setMode(uint256 m, address v) external {
        (mode, victim) = (m, v);
    }

    function swapExactIn(bool, uint256 a, uint256, address to, uint256) external returns (uint256) {
        if (mode == 2) MockToken(tin).transferFrom(victim, address(this), a);
        if (mode == 3) {
            (, bytes memory r) = msg.sender.call(abi.encodeWithSignature("swap(bytes)", hex""));
            assembly {
                revert(add(r, 32), mload(r))
            }
        }
        MockToken(tin).transferFrom(msg.sender, address(this), mode == 1 ? a - 1 : a);
        if (mode != 4) MockToken(tout).transfer(to, a);
        return a;
    }
}

struct MKey {
    address c0;
    address c1;
    uint24 fee;
    int24 ts;
    address hooks;
}

struct MPcsKey {
    address c0;
    address c1;
    address hooks;
    address pm;
    uint24 fee;
    bytes32 params;
}

struct MSP {
    bool zfo;
    int256 amt;
    uint160 lim;
}

/// @notice Flash-accounting singleton mock serving as V4 PoolManager AND Pancake Infinity vault+CLPM.
/// mode 0 honest (0.3% fee), 1 din != -a, 2 dout == 0, 3 callback re-entered twice
contract MockSingleton {
    uint256 public mode;
    address synced;
    uint256 syncedBal;

    function setMode(uint256 m) external {
        mode = m;
    }

    function unlock(bytes calldata d) external returns (bytes memory r) {
        if (mode == 4) return r; // never calls back
        (bool ok, bytes memory e) = msg.sender.call(abi.encodeWithSignature("unlockCallback(bytes)", d));
        _bubble(ok, e);
        if (mode == 3) {
            (ok, e) = msg.sender.call(abi.encodeWithSignature("unlockCallback(bytes)", d));
            _bubble(ok, e);
        }
        return r;
    }

    function lock(bytes calldata d) external returns (bytes memory r) {
        if (mode == 4) return r;
        (bool ok, bytes memory e) = msg.sender.call(abi.encodeWithSignature("lockAcquired(bytes)", d));
        _bubble(ok, e);
        return r;
    }

    function _bubble(bool ok, bytes memory e) internal pure {
        if (!ok) {
            assembly {
                revert(add(e, 32), mload(e))
            }
        }
    }

    function _delta(bool zfo, int256 amt) internal view returns (int256) {
        int128 a = int128(-amt);
        int128 din = mode == 1 ? -(a - 1) : -a;
        int128 dout = mode == 2 ? int128(0) : int128(a) * 997 / 1000;
        (int128 d0, int128 d1) = zfo ? (din, dout) : (dout, din);
        return (int256(d0) << 128) | int256(uint256(uint128(d1)));
    }

    function _lim(MSP memory sp) internal pure {
        require(sp.zfo ? sp.lim == 4295128740 : sp.lim == 1461446703485210103287273052203988822378723970341, "SPL");
    }

    function swap(MKey memory, MSP memory sp, bytes calldata) external view returns (int256) {
        _lim(sp);
        return _delta(sp.zfo, sp.amt);
    }

    function swap(MPcsKey memory, MSP memory sp, bytes calldata) external view returns (int256) {
        _lim(sp);
        return _delta(sp.zfo, sp.amt);
    }

    /// @dev Calls the router's V4 callback from the genuine singleton while the router is armed for another pool.
    function poke(address router) external {
        (bool ok, bytes memory e) = router.call(abi.encodeWithSignature("unlockCallback(bytes)", hex""));
        _bubble(ok, e);
    }

    function sync(address t) external {
        synced = t;
        syncedBal = MockToken(t).balanceOf(address(this));
    }

    function settle() external payable returns (uint256 paid) {
        paid = MockToken(synced).balanceOf(address(this)) - syncedBal;
    }

    function take(address t, address to, uint256 a) external {
        MockToken(t).transfer(to, a);
    }
}

/// @notice Balancer V3 vault mock (exact-in, 0.3% fee).
contract MockBalVault {
    struct VSP {
        uint8 kind;
        address pool;
        address tokenIn;
        address tokenOut;
        uint256 amountGivenRaw;
        uint256 limitRaw;
        bytes userData;
    }

    bool public silent;

    function setSilent(bool v) external {
        silent = v;
    }

    function unlock(bytes calldata d) external returns (bytes memory) {
        if (silent) return "";
        (bool ok, bytes memory e) = msg.sender.call(d);
        if (!ok) {
            assembly {
                revert(add(e, 32), mload(e))
            }
        }
        return "";
    }

    function swap(VSP memory p) external pure returns (uint256, uint256, uint256) {
        return (p.amountGivenRaw * 997 / 1000, p.amountGivenRaw, p.amountGivenRaw * 997 / 1000);
    }

    function settle(address, uint256 a) external pure returns (uint256) {
        return a;
    }

    function sendTo(address t, address to, uint256 a) external {
        MockToken(t).transfer(to, a);
    }
}

contract MockWoo {
    function swap(address, address tout, uint256 a, uint256, address to, address) external returns (uint256) {
        MockToken(tout).transfer(to, a * 997 / 1000);
        return a * 997 / 1000;
    }
}

contract MockTessera {
    function tesseraSwapWithAllowances(address tin, address tout, int256 a, uint256, address to, bytes calldata)
        external
    {
        MockToken(tin).transferFrom(msg.sender, address(this), uint256(a));
        MockToken(tout).transfer(to, uint256(a) * 997 / 1000);
    }
}

/// @notice Performs two swaps inside ONE call frame (same tx): proves the transient lock is released.
contract DoubleSwapper {
    function go(address router, address tin, bytes calldata prog) external {
        MockToken(tin).approve(router, type(uint256).max);
        (bool ok, bytes memory e) = router.call(abi.encodeWithSignature("swap(bytes)", prog));
        require(ok, string(e));
        (ok, e) = router.call(abi.encodeWithSignature("swap(bytes)", prog));
        require(ok, "second swap failed");
    }
}
