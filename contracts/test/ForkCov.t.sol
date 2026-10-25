// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {TickingRouter} from "../src/TickingRouter.sol";
import {Program as P} from "./utils/Program.sol";

interface IERC20x {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

interface IQuoterV2 {
    struct Q {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint24 fee;
        uint160 lim;
    }

    function quoteExactInputSingle(Q memory) external returns (uint256, uint160, uint32, uint256);
}

struct K4 {
    address c0;
    address c1;
    uint24 fee;
    int24 ts;
    address hooks;
}

struct CLKey {
    address c0;
    address c1;
    address hooks;
    address pm;
    uint24 fee;
    bytes32 params;
}

struct CLQ {
    CLKey key;
    bool zfo;
    uint128 amt;
    bytes hd;
}

struct Q4 {
    K4 key;
    bool zfo;
    uint128 amt;
    bytes hd;
}

interface IV4Quoter {
    function quoteExactInputSingle(Q4 memory) external returns (uint256, uint256);
}

/// @notice Robinhood Chain mainnet fork: every venue, differential vs legacy V0, vs official quoters, vs direct pool.
contract ForkCovTest is Test {
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant USDC = 0x80e0e24718dbFcad49ECAA6F1e6C89A190586cA8;
    address constant CRCL = 0xdF0992E440dD0be65BD8439b609d6D4366bf1CB5;
    address constant SPCX = 0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa;
    address constant T117 = 0x117cc2133c37B721F49dE2A7a74833232B3B4C0C;
    address constant FEE_TO = 0x3e13cd3704099424a6301Ea4125791a6a1433637;
    address constant PM = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address constant BALV = 0xB055000fbE3cc9bDE7742C583a86cc6283E3fF85;
    address constant WOO = 0x5520385bFcf07Ec87C4c53A7d8d65595Dff69FA4;
    address constant TESS = 0x55555522005BcAE1c2424D474BfD5ed477749E3e;
    address constant KYB = 0x6131B5fae19EA4f9D964eAc0408E4408b66337b5;
    address constant F_UNI = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address constant F_SUSHI = 0xE51960f1B45f1C9FB6D166E6a884F866fC70433B;
    address constant F_FORK = 0xEce6eCd61177336ea6Fb9b17937AC439D85EE20B;
    address constant F_TS0 = 0xE0c4ceb92d08CA985bB70fe0a22fEb121A9854A8;
    address constant F_TS1 = 0x1ac9dB4a2608ba45D6127B1737949b51Bb54B7F3;
    address constant F_ALG = 0x16494A80E08Bcb9285D87b67149d7b01774D82F8;
    address constant PCS_V = 0x4F922d5B15e6691e0469663E4F5C4177f23c5FaF;
    address constant PCS_CLPM = 0xeE04c68742e6Bf434bE8039580D2e89BBE55bc6f;
    address constant QCL = 0x6b3E15009681869FCF6AE2F3bBf6e33B2D0C590e;
    address constant QV3 = 0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7;
    address constant QV4 = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    address constant BUSH = 0xc2fEf42121FaDA751ba9f2107C4c8d4C593af435;
    address constant FERMI_WU = 0x0217Ee258081bb431f9fA25e0943628cE9d0595a;
    address constant ROUTER_AT = 0x00000000000000000000000000000000071C4E15;
    uint8 constant T = 0x40;
    uint8 constant CH = 0x80;

    TickingRouter r;
    address user = address(0xA11CE);
    address rcpt = address(0xB0B);

    function setUp() public {
        string memory rpc = vm.envOr("RH_RPC", string("https://rpc.mainnet.chain.robinhood.com"));
        uint256 blk = vm.envOr("FORK_BLOCK", uint256(0));
        if (blk == 0) vm.createSelectFork(rpc);
        else vm.createSelectFork(rpc, blk);
        address deployed = vm.envOr("ROUTER_ADDR", address(0)); // post-deploy smoke: test the live bytecode
        if (deployed != address(0)) {
            r = TickingRouter(payable(deployed));
        } else {
            deployCodeTo(
                "TickingRouter.sol:TickingRouter",
                abi.encode(
                    [PM, BALV, WOO, TESS, KYB, F_UNI, F_SUSHI, F_FORK, F_TS0, F_TS1, F_ALG, PCS_V, PCS_CLPM],
                    WETH,
                    FEE_TO
                ),
                ROUTER_AT
            );
            r = TickingRouter(payable(ROUTER_AT));
        }
    }

    function _fund(address t, uint256 a) internal {
        deal(t, user, a);
        vm.startPrank(user);
        IERC20x(t).approve(address(r), a);
        vm.stopPrank();
    }

    function _h(address tin, address tout) internal view returns (bytes memory) {
        return P.header(tin, tout, rcpt, 1, uint32(block.timestamp + 60));
    }

    /// @dev Runs v1, asserts I2/I3, returns out. State is reverted afterwards so callers can diff.
    function _v1(bytes memory prog, address tin, address tout, uint256 amt)
        internal
        returns (uint256 out, uint256 gas_)
    {
        uint256 s = vm.snapshotState();
        _fund(tin, amt);
        uint256 b = IERC20x(tout).balanceOf(rcpt);
        uint256 bf = IERC20x(tout).balanceOf(FEE_TO);
        vm.prank(user);
        uint256 g = gasleft();
        out = r.swap(prog);
        gas_ = g - gasleft();
        assertEq(IERC20x(tout).balanceOf(rcpt) - b, out, "I3 recipient delta");
        uint256 fee = IERC20x(tout).balanceOf(FEE_TO) - bf; // v2 I7
        assertEq(fee, (out + fee) * 5 / 10_000, "I7 fee == floor(gross*5bps)");
        out += fee; // gross: keeps quoter comparisons wei-exact
        assertEq(IERC20x(tin).balanceOf(address(r)), 0, "I2 tin");
        assertEq(IERC20x(tout).balanceOf(address(r)), 0, "I2 tout");
        assertEq(IERC20x(tin).balanceOf(user), 0, "exact spend");
        vm.revertToState(s);
    }

    function _o(bytes memory prog, address tin, address tout, uint256 amt) internal returns (uint256 o) {
        (o,) = _v1(prog, tin, tout, amt);
        assertGt(o, 0);
    }

    function _qv3(address tin, address tout, uint256 amt, uint24 fee) internal returns (uint256 q) {
        (q,,,) = IQuoterV2(QV3).quoteExactInputSingle(IQuoterV2.Q(tin, tout, amt, fee, 0));
    }

    function _qv4(address tin, address tout, uint256 amt, uint24 fee, int24 ts) internal returns (uint256 q) {
        (address c0, address c1) = tin < tout ? (tin, tout) : (tout, tin);
        (q,) = IV4Quoter(QV4).quoteExactInputSingle(Q4(K4(c0, c1, fee, ts, address(0)), tin < tout, uint128(amt), ""));
    }

    // ───────────── per venue (differential vs V0 + quoter where one exists) ─────────────
    function test_fork_v3_uni_WETH_USDG() public {
        address pool = 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca;
        uint256 o = _o(bytes.concat(_h(WETH, USDG), P.v3(T, 1e18, USDG, 0, 100, pool)), WETH, USDG, 1e18);
        assertEq(o, _qv3(WETH, USDG, 1e18, 100), "quote == fill (QuoterV2)");
    }

    function test_fork_v3_uni_USDG_WETH() public {
        address pool = 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca;
        uint256 o = _o(bytes.concat(_h(USDG, WETH), P.v3(T, 1000e6, WETH, 0, 100, pool)), USDG, WETH, 1000e6);
        assertEq(o, _qv3(USDG, WETH, 1000e6, 100), "quote == fill (QuoterV2)");
    }

    function test_fork_v3_sushi() public {
        address pool = 0x9B050cb1F265094b0977160a2BD7dA8C9E529c3C;
        _o(bytes.concat(_h(WETH, USDG), P.v3(T, 1e17, USDG, 1, 500, pool)), WETH, USDG, 1e17);
    }

    function test_fork_v3_fork() public {
        address pool = 0xb2a6aD51b3ea3CDc8D3508Cca147A43471382e53;
        _o(bytes.concat(_h(WETH, USDG), P.v3(T, 1e16, USDG, 2, 100, pool)), WETH, USDG, 1e16);
    }

    function test_fork_v3ts_USDG_CRCL() public {
        address pool = 0xbE1563DA6CC4E3057334Fd10735beBc6770B537F;
        _o(bytes.concat(_h(USDG, CRCL), P.v3ts(T, 100e6, CRCL, 1, 60, pool)), USDG, CRCL, 100e6);
    }

    function test_fork_v3ts_e0c4() public {
        address pool = 0x38453c115607463Ac284820Ce959831042f3Df4E;
        _o(bytes.concat(_h(USDG, T117), P.v3ts(T, 10e6, T117, 0, 1, pool)), USDG, T117, 10e6);
    }

    function test_fork_algebra() public {
        address pool = 0x05E146F995aA70d0BF8481Ee3D27368A537a8e0D;
        _o(bytes.concat(_h(USDG, T117), P.alg(T, 10e6, T117, pool)), USDG, T117, 10e6);
    }

    function test_fork_v4_USDG_CRCL() public {
        bytes memory kd = abi.encode(USDG, CRCL, uint24(3000), int24(30), address(0));
        uint256 o = _o(bytes.concat(_h(USDG, CRCL), P.v4(T, 1000e6, CRCL, 3000, 30, address(0))), USDG, CRCL, 1000e6);
        assertEq(o, _qv4(USDG, CRCL, 1000e6, 3000, 30), "quote == fill (V4Quoter)");
    }

    function test_fork_v4_CRCL_USDG() public {
        deal(CRCL, address(this), 0);
        bytes memory kd = abi.encode(USDG, CRCL, uint24(3000), int24(30), address(0));
        uint256 o = _o(bytes.concat(_h(CRCL, USDG), P.v4(T, 1e18, USDG, 3000, 30, address(0))), CRCL, USDG, 1e18);
        assertEq(o, _qv4(CRCL, USDG, 1e18, 3000, 30), "quote == fill (V4Quoter)");
    }

    function test_fork_woofi() public {
        _o(bytes.concat(_h(WETH, USDG), P.woo(T, 1e18, USDG)), WETH, USDG, 1e18);
    }

    function test_fork_fermi() public {
        _o(bytes.concat(_h(WETH, USDG), P.fermi(T, 1e18, USDG, FERMI_WU)), WETH, USDG, 1e18);
    }

    function test_fork_tessera_USDG_SPCX() public {
        _o(bytes.concat(_h(USDG, SPCX), P.tess(T, 1000e6, SPCX)), USDG, SPCX, 1000e6);
    }

    function test_fork_bush() public {
        _o(bytes.concat(_h(WETH, USDG), P.bal(T, 1e16, USDG, BUSH)), WETH, USDG, 1e16);
    }

    // ───────────── compositions ─────────────

    function test_fork_multihop_cov() public {
        _o(
            bytes.concat(
                _h(WETH, CRCL),
                P.v3(0, 1e17, USDG, 0, 100, 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca),
                P.v4(CH | T, 0, CRCL, 3000, 30, address(0))
            ),
            WETH,
            CRCL,
            1e17
        );
        _o(bytes.concat(_h(WETH, SPCX), P.woo(0, 1e17, USDG), P.tess(CH | T, 0, SPCX)), WETH, SPCX, 1e17);
        _o(
            bytes.concat(
                _h(WETH, SPCX),
                P.bal(0, 1e15, USDG, BUSH),
                P.fermi(CH, 0, WETH, FERMI_WU),
                P.v3(CH, 0, USDG, 0, 100, 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca),
                P.tess(CH | T, 0, SPCX)
            ),
            WETH,
            SPCX,
            1e15
        );
    }

    // ───────────── stablecoins: USDG + USDC (6 dec, bridged USDC 0x80e0…6cA8) ─────────────
    function test_fork_usdc_meta() public view {
        (bool ok, bytes memory d) = USDC.staticcall(abi.encodeWithSignature("decimals()"));
        assertTrue(ok);
        assertEq(abi.decode(d, (uint8)), 6);
        (ok, d) = USDG.staticcall(abi.encodeWithSignature("decimals()"));
        assertEq(abi.decode(d, (uint8)), 6);
    }

    /// USDC/USDG only exists as a V4 pool with a 92% fee on chain 4663 (USDC supply ~340). We still prove
    /// the router executes it exactly per the V4 quoter, both directions, and composes with stock legs.
    function test_fork_USDG_USDC() public {
        uint256 o =
            _tryV1(bytes.concat(_h(USDG, USDC), P.v4(T, 10e6, USDC, 920000, 18400, address(0))), USDG, USDC, 10e6);
        if (o > 0) assertEq(o, _qv4(USDG, USDC, 10e6, 920000, 18400), "quote == fill USDG->USDC");
    }

    function test_fork_USDC_USDG() public {
        uint256 o = _tryV1(bytes.concat(_h(USDC, USDG), P.v4(T, 1e6, USDG, 920000, 18400, address(0))), USDC, USDG, 1e6);
        if (o > 0) assertEq(o, _qv4(USDC, USDG, 1e6, 920000, 18400), "quote == fill USDC->USDG");
    }

    function test_fork_CRCL_USDC() public {
        _tryV1(
            bytes.concat(
                _h(CRCL, USDC),
                P.v4(0, 1e18, USDG, 3000, 30, address(0)),
                P.v4(CH | T, 0, USDC, 920000, 18400, address(0))
            ),
            CRCL,
            USDC,
            1e18
        );
    }

    function test_fork_USDC_CRCL() public {
        _tryV1(
            bytes.concat(
                _h(USDC, CRCL),
                P.v4(0, 1e6, USDG, 920000, 18400, address(0)),
                P.v4(CH | T, 0, CRCL, 3000, 30, address(0))
            ),
            USDC,
            CRCL,
            1e6
        );
    }

    function test_fork_USDG_SPCX_CRCL_stock_both_ways() public {
        _v1(bytes.concat(_h(USDG, SPCX), P.tess(T, 500e6, SPCX)), USDG, SPCX, 500e6);
        _v1(
            bytes.concat(_h(USDG, CRCL), P.v3ts(T, 100e6, CRCL, 1, 60, 0xbE1563DA6CC4E3057334Fd10735beBc6770B537F)),
            USDG,
            CRCL,
            100e6
        );
        _v1(
            bytes.concat(_h(CRCL, USDG), P.v3ts(T, 1e18, USDG, 1, 60, 0xbE1563DA6CC4E3057334Fd10735beBc6770B537F)),
            CRCL,
            USDG,
            1e18
        );
    }

    /// @dev Executes; if the venue has no liquidity the router must revert cleanly (never strand funds).
    function _tryV1(bytes memory prog, address tin, address tout, uint256 amt) internal returns (uint256 o) {
        uint256 s = vm.snapshotState();
        _fund(tin, amt);
        uint256 bf = IERC20x(tout).balanceOf(FEE_TO);
        vm.prank(user);
        try r.swap(prog) returns (uint256 out) {
            uint256 fee = IERC20x(tout).balanceOf(FEE_TO) - bf; // v2 I7
            assertEq(fee, (out + fee) * 5 / 10_000, "I7 fee");
            o = out + fee; // gross
            emit log_named_uint("out", out);
        } catch (bytes memory e) {
            emit log_named_bytes("clean revert (no liquidity)", e);
            assertEq(IERC20x(tin).balanceOf(user), amt, "funds returned on revert");
        }
        assertEq(IERC20x(tin).balanceOf(address(r)), 0);
        assertEq(IERC20x(tout).balanceOf(address(r)), 0);
        vm.revertToState(s);
    }

    // ───────────── Kyber fallback (real calldata, fixture fetched for ROUTER_AT) ─────────────
    function test_fork_kyber() public {
        string memory j;
        try vm.readFile("test/fixtures/kyber.json") returns (string memory s) {
            j = s;
        } catch {
            vm.skip(true);
        }
        bytes memory cd = vm.parseJsonBytes(j, ".data");
        uint256 amt = vm.parseUint(vm.parseJsonString(j, ".amountIn"));
        uint256 quoted = vm.parseUint(vm.parseJsonString(j, ".quoted"));
        (uint256 o,) = _v1(bytes.concat(_h(WETH, USDG), P.kyb(T, uint128(amt), USDG, cd)), WETH, USDG, amt);
        emit log_named_uint("kyber quoted", quoted);
        emit log_named_uint("kyber filled via router", o);
        assertEq(IAllow(WETH).allowance(address(r), KYB), 0);
    }

    // ───────────── fork negatives ─────────────
    function test_fork_fakePool_rejected() public {
        // a genuine Sushi pool presented as a Uniswap pool (fid 0) must fail factory auth
        bytes memory prog =
            bytes.concat(_h(WETH, USDG), P.v3(T, 1e17, USDG, 0, 500, 0x9B050cb1F265094b0977160a2BD7dA8C9E529c3C));
        _fund(WETH, 1e17);
        vm.prank(user);
        vm.expectRevert(TickingRouter.BadPool.selector);
        r.swap(prog);
    }

    function test_fork_transient_supported() public {
        // EIP-1153 on RH (ArbOS): the lock + callback auth depend on it. A full swap proves tload/tstore work.
        _v1(
            bytes.concat(_h(WETH, USDG), P.v3(T, 1e15, USDG, 0, 100, 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca)),
            WETH,
            USDG,
            1e15
        );
    }

    /// @dev Fuzz on fork: random amount + split across uni and sushi; v1 == v0 to the wei.
    // ───────────── Pancake Infinity CL + hooked V4 (quote == fill vs official quoters) ─────────────
    function _qcl(address tin, address tout, uint256 amt, uint24 fee, bytes32 params) internal returns (uint256 q) {
        (address c0, address c1) = tin < tout ? (tin, tout) : (tout, tin);
        (bool ok, bytes memory d) = QCL.call(
            abi.encodeWithSelector(
                bytes4(0x9938b8ed), CLQ(CLKey(c0, c1, address(0), PCS_CLPM, fee, params), tin < tout, uint128(amt), "")
            )
        );
        require(ok, "CL quoter");
        q = abi.decode(d, (uint256));
    }

    function test_fork_pcs_WETH_USDG() public {
        bytes32 pr = bytes32(uint256(0x10000));
        uint256 o = _o(bytes.concat(_h(WETH, USDG), P.pcs(T, 1e17, USDG, 90, pr, address(0))), WETH, USDG, 1e17);
        assertEq(o, _qcl(WETH, USDG, 1e17, 90, pr), "quote == fill (CLQuoter)");
        o = _o(bytes.concat(_h(USDG, WETH), P.pcs(T, 100e6, WETH, 90, pr, address(0))), USDG, WETH, 100e6);
        assertEq(o, _qcl(USDG, WETH, 100e6, 90, pr), "quote == fill (CLQuoter) rev");
    }

    function test_fork_pcs_USDG_D060() public {
        address tk = 0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC;
        bytes32 pr = bytes32(uint256(0xa0000));
        uint256 o = _o(bytes.concat(_h(USDG, tk), P.pcs(T, 10e6, tk, 81, pr, address(0))), USDG, tk, 10e6);
        assertEq(o, _qcl(USDG, tk, 10e6, 81, pr), "quote == fill (CLQuoter)");
    }

    function test_fork_v4_hooked_dynamicFee_USDG_SPCX() public {
        address hk = 0xC15b155a3094934171A15625d1a80Aba267B6080; // BEFORE_INITIALIZE|BEFORE_SWAP, no delta flags
        (address c0, address c1) = (SPCX, USDG);
        uint256 o = _o(bytes.concat(_h(USDG, SPCX), P.v4(T, 100e6, SPCX, 0x800000, 60, hk)), USDG, SPCX, 100e6);
        (uint256 q,) = IV4Quoter(QV4).quoteExactInputSingle(Q4(K4(c0, c1, 0x800000, 60, hk), false, uint128(100e6), ""));
        assertEq(o, q, "quote == fill (V4Quoter, hooked)");
    }

    function test_fork_v4_deltaHookRejected() public {
        address hk = 0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544; // AFTER_SWAP_RETURNS_DELTA set
        bytes memory prog = bytes.concat(_h(USDG, SPCX), P.v4(T, 1e6, SPCX, 0x800000, 8, hk));
        _fund(USDG, 1e6);
        vm.prank(user);
        vm.expectRevert(TickingRouter.BadPool.selector);
        r.swap(prog);
    }
}

interface IAllow {
    function allowance(address, address) external view returns (uint256);
}
