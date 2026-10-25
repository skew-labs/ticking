// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {TickingRouter} from "../src/TickingRouter.sol";
import {Program as P} from "./utils/Program.sol";
import {
    MockToken,
    MockV3Factory,
    MockV3Pool,
    EvilCaller,
    MockFermi,
    MockSingleton,
    DoubleSwapper,
    MockBalVault,
    MockWoo,
    MockTessera
} from "./utils/Mocks.sol";
import {MockKyber} from "./utils/MockKyber.sol";

contract Base is Test {
    TickingRouter r;
    MockV3Factory fac;
    MockToken A;
    MockToken B;
    MockToken C;
    MockV3Pool pAB;
    MockV3Pool pAB2;
    MockV3Pool pBC;
    MockFermi fermi;
    MockKyber kyb;
    MockSingleton pm;
    MockBalVault bv;
    MockWoo woo;
    MockTessera tess;
    MockV3Factory facTs;
    MockV3Factory facAlg;
    MockV3Pool pTs;
    MockV3Pool pAlg;
    address user = address(0xA11CE);
    address rcpt = address(0xB0B);
    address victim = address(0x71C7);

    function setUp() public virtual {
        fac = new MockV3Factory();
        kyb = new MockKyber();
        address d = address(0xdead);
        pm = new MockSingleton();
        bv = new MockBalVault();
        woo = new MockWoo();
        tess = new MockTessera();
        facTs = new MockV3Factory();
        facAlg = new MockV3Factory();
        r = new TickingRouter(
            [
                address(pm),
                address(bv),
                address(woo),
                address(tess),
                address(kyb),
                address(fac),
                d,
                d,
                address(facTs),
                d,
                address(facAlg),
                address(pm),
                address(pm)
            ]
        );
        A = new MockToken();
        B = new MockToken();
        C = new MockToken();
        pAB = _pool(A, B, 100);
        pAB2 = _pool(A, B, 500);
        pBC = _pool(B, C, 100);
        pTs = new MockV3Pool(address(A), address(B));
        facTs.set(pTs.token0(), pTs.token1(), 60, address(pTs));
        pAlg = new MockV3Pool(address(A), address(B));
        pAlg.setSel(0x2c8958f6);
        facAlg.set(pAlg.token0(), pAlg.token1(), 0, address(pAlg));
        for (uint256 k; k < 2; ++k) {
            address v = k == 0 ? address(pTs) : address(pAlg);
            A.mint(v, 1e30);
            B.mint(v, 1e30);
        }
        B.mint(address(pm), 1e30);
        B.mint(address(bv), 1e30);
        B.mint(address(woo), 1e30);
        B.mint(address(tess), 1e30);
        fermi = new MockFermi(address(A), address(B));
        B.mint(address(fermi), 1e30);
        B.mint(address(kyb), 1e30);
        A.mint(user, 1e30);
        vm.prank(user);
        A.approve(address(r), type(uint256).max);
        A.mint(victim, 1e30);
        vm.prank(victim);
        A.approve(address(r), type(uint256).max); // victim has a max approval to the router: must never be usable by others
    }

    function _pool(MockToken x, MockToken y, uint24 fee) internal returns (MockV3Pool p) {
        p = new MockV3Pool(address(x), address(y));
        fac.set(p.token0(), p.token1(), fee, address(p));
        x.mint(address(p), 1e30);
        y.mint(address(p), 1e30);
    }

    function _h(MockToken tin, MockToken tout, uint128 minOut) internal view returns (bytes memory) {
        return P.header(address(tin), address(tout), rcpt, minOut, uint32(block.timestamp + 60));
    }

    function _run(bytes memory prog) internal returns (uint256) {
        vm.prank(user);
        return r.swap(prog);
    }

    function _clean() internal view {
        assertEq(A.balanceOf(address(r)), 0);
        assertEq(B.balanceOf(address(r)), 0);
        assertEq(C.balanceOf(address(r)), 0);
        assertEq(A.allowance(address(r), address(fermi)), 0);
        assertEq(A.allowance(address(r), address(kyb)), 0);
    }
}

contract UnitTest is Base {
    uint8 constant T = 0x40;
    uint8 constant CH = 0x80;

    // ───────────── happy paths ─────────────
    function test_single() public {
        uint256 o = _run(bytes.concat(_h(A, B, 997), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
        assertEq(o, 997);
        assertEq(B.balanceOf(rcpt), 997);
        _clean();
    }

    function test_split() public {
        bytes memory prog = bytes.concat(
            _h(A, B, 0),
            P.v3(T, 6000, address(B), 0, 100, address(pAB)),
            P.v3(T, 4000, address(B), 0, 500, address(pAB2))
        );
        assertEq(_run(prog), 5982 + 3988);
        assertEq(A.balanceOf(user), 1e30 - 10_000);
        _clean();
    }

    function test_multihop() public {
        bytes memory prog = bytes.concat(
            _h(A, C, 0),
            P.v3(0, 10_000, address(B), 0, 100, address(pAB)),
            P.v3(CH | T, 0, address(C), 0, 100, address(pBC))
        );
        assertEq(_run(prog), uint256(10_000) * 997 / 1000 * 997 / 1000);
        _clean();
    }

    function test_allCallbackSelectors() public {
        bytes4[4] memory s = [bytes4(0xfa461e33), 0x2c8958f6, 0x23a69e75, 0x654b6487];
        for (uint256 i; i < 4; ++i) {
            pAB.setSel(s[i]);
            _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
        }
        _clean();
    }

    function test_fermiAndKyber() public {
        _run(bytes.concat(_h(A, B, 1000), P.fermi(T, 1000, address(B), address(fermi))));
        bytes memory cd =
            abi.encodePacked(bytes4(0xe21fd0e9), abi.encode(address(A), address(B), 1000, 990, address(r), false));
        assertEq(_run(bytes.concat(_h(A, B, 990), P.kyb(T, 1000, address(B), cd))), 990);
        _clean();
    }

    // ───────────── adversarial pools ─────────────
    function test_poolAsksMore() public {
        pAB.setMode(1);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_poolCallbackTwice() public {
        pAB.setMode(2);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_poolNoCallback() public {
        pAB.setMode(3);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_poolPartialFill() public {
        pAB.setMode(4);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_poolLiesAboutOutput_minOutHolds() public {
        pAB.setMode(5); // reports 1000x the real output
        vm.expectRevert(abi.encodeWithSelector(TickingRouter.Slippage.selector, 1, 2));
        _run(bytes.concat(_h(A, B, 2), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_fakePoolNotInFactory() public {
        MockV3Pool fake = new MockV3Pool(address(A), address(B));
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(fake))));
    }

    function test_wrongFeeForPool() public {
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 3000, address(pAB))));
    }

    function test_unknownFactoryId() public {
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 7, 100, address(pAB))));
    }

    function test_unarmedCallback() public {
        EvilCaller e = new EvilCaller();
        assertFalse(e.hit(address(r), 1e18, 0));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.unlockCallback("");
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.balancerCallback(address(0), address(B), rcpt);
        (bool ok,) = address(r).call(hex"deadbeef");
        assertFalse(ok);
    }

    // ───────────── adversarial tokens ─────────────
    function test_reentrantToken() public {
        A.setReenter(address(r), abi.encodeCall(TickingRouter.swap, (hex"")));
        // re-entry hits the lock (Reentrancy) inside the token; the router normalizes it to TransferFailed
        vm.expectRevert(TickingRouter.TransferFailed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_feeOnTransferIn_v3() public {
        A.setFee(100);
        vm.expectRevert(bytes("IIA"));
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_feeOnTransferIn_pull() public {
        A.setFee(100);
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(_h(A, B, 0), P.fermi(T, 1000, address(B), address(fermi))));
    }

    function test_feeOnTransferOut_minOutOnDelta() public {
        B.setFee(100); // 1% fee on output leg: recipient delta is what counts
        vm.expectRevert(abi.encodeWithSelector(TickingRouter.Slippage.selector, 988, 997));
        _run(bytes.concat(_h(A, B, 997), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_falseReturningToken() public {
        A.setReturnFalse(true);
        vm.expectRevert(TickingRouter.TransferFailed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_blacklistedRecipient() public {
        B.setBlacklist(rcpt, true);
        vm.expectRevert(bytes("blacklisted"));
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
        _clean();
        assertEq(A.balanceOf(user), 1e30);
    }

    function test_reentrancyLock() public {
        fermi.setMode(3, address(0)); // venue re-enters router.swap mid-route
        vm.expectRevert(TickingRouter.Reentrancy.selector);
        _run(bytes.concat(_h(A, B, 0), P.fermi(T, 1000, address(B), address(fermi))));
    }

    function test_pausedToken() public {
        A.setPaused(true);
        vm.expectRevert(TickingRouter.TransferFailed.selector); // token revert reasons are normalized
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_noCodeToken() public {
        bytes memory prog = bytes.concat(
            P.header(address(0x1234), address(B), rcpt, 0, uint32(block.timestamp + 1)),
            P.fermi(T, 1000, address(B), address(fermi))
        );
        vm.expectRevert(TickingRouter.TransferFailed.selector);
        _run(prog);
    }

    // ───────────── approval-venue sandbox ─────────────
    function test_fermiPullsLess() public {
        fermi.setMode(1, address(0));
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.fermi(T, 1000, address(B), address(fermi))));
    }

    function test_venueCannotUseVictimApproval() public {
        fermi.setMode(2, victim);
        vm.expectRevert(bytes("allow"));
        _run(bytes.concat(_h(A, B, 0), P.fermi(T, 1000, address(B), address(fermi))));
        assertEq(A.balanceOf(victim), 1e30);
    }

    function test_kyberBadSelector() public {
        bytes memory cd = abi.encodeWithSelector(bytes4(0x23b872dd), victim, user, 1e18); // transferFrom
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), cd)));
    }

    function test_kyberPullsLess() public {
        bytes memory cd =
            abi.encodePacked(bytes4(0xe21fd0e9), abi.encode(address(A), address(B), 1000, 990, address(r), true));
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), cd)));
    }

    function test_kyberSendsElsewhere() public {
        // calldata routes output to attacker: router measures its own delta = 0 -> minOut fails
        bytes memory cd =
            abi.encodePacked(bytes4(0xe21fd0e9), abi.encode(address(A), address(B), 1000, 990, address(0xBAD), false));
        vm.expectRevert(abi.encodeWithSelector(TickingRouter.Slippage.selector, 0, 1));
        _run(bytes.concat(_h(A, B, 1), P.kyb(T, 1000, address(B), cd)));
    }

    function test_kyberTruncated() public {
        bytes memory prog = bytes.concat(
            _h(A, B, 0), abi.encodePacked(uint8(9 | T), uint128(1000), address(B), uint16(500), bytes4(0xe21fd0e9))
        );
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(prog);
    }

    // ───────────── header / program validation ─────────────
    function test_expired() public {
        bytes memory prog = bytes.concat(
            P.header(address(A), address(B), rcpt, 0, uint32(block.timestamp - 1)),
            P.v3(T, 1000, address(B), 0, 100, address(pAB))
        );
        vm.expectRevert(TickingRouter.Expired.selector);
        _run(prog);
    }

    function test_slippage() public {
        vm.expectRevert(abi.encodeWithSelector(TickingRouter.Slippage.selector, 997, 998));
        _run(bytes.concat(_h(A, B, 998), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_zeroAmount() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 0, address(B), 0, 100, address(pAB))));
    }

    function test_badRecipients() public {
        bytes memory op = P.v3(T, 1000, address(B), 0, 100, address(pAB));
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(P.header(address(A), address(B), address(0), 0, type(uint32).max), op));
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(P.header(address(A), address(B), address(r), 0, type(uint32).max), op));
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(P.header(address(A), address(A), rcpt, 0, type(uint32).max), op));
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(P.header(address(0), address(B), rcpt, 0, type(uint32).max), op));
    }

    function test_identity_terminalMustBeTokenOut() public {
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(_h(A, C, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_identity_nonTerminalCannotBeTokenOut() public {
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(0, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_identity_toutEqualsTin() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(A), 0, 100, address(pAB))));
    }

    function test_chainWithoutPending() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(CH | T, 0, address(B), 0, 100, address(pAB))));
    }

    function test_pendingNotConsumed() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, C, 0), P.v3(0, 1000, address(B), 0, 100, address(pAB))));
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(
            bytes.concat(
                _h(A, C, 0),
                P.v3(0, 1000, address(B), 0, 100, address(pAB)),
                P.v3(T, 1000, address(C), 0, 100, address(pAB))
            )
        );
    }

    function test_emptyAndTruncated() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(_h(A, B, 0));
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(hex"00");
        bytes memory op = P.v3(T, 1000, address(B), 0, 100, address(pAB));
        for (uint256 cut = 1; cut < op.length; ++cut) {
            bytes memory t = new bytes(op.length - cut);
            for (uint256 k; k < t.length; ++k) {
                t[k] = op[k];
            }
            vm.expectRevert(TickingRouter.Malformed.selector);
            _run(bytes.concat(_h(A, B, 0), t));
        }
    }

    function test_badOpcodes() public {
        uint8[7] memory ops = [0, 10, 11, 12, 13, 14, 15];
        for (uint256 k; k < ops.length; ++k) {
            bytes memory prog = bytes.concat(
                _h(A, B, 0), abi.encodePacked(uint8(ops[k] | T), uint128(1000), address(B), address(pAB), bytes32(0))
            );
            vm.expectRevert(TickingRouter.Malformed.selector);
            _run(prog);
        }
    }

    function test_constructorZero() public {
        address d = address(1);
        vm.expectRevert(TickingRouter.Malformed.selector);
        new TickingRouter([d, d, d, d, d, d, d, d, d, d, d, d, address(0)]);
    }

    function test_nonexistentPool() public {
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 3000, address(0))));
    }

    function test_venueTargetIsToken() public {
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.fermi(T, 1000, address(B), address(A))));
    }

    function test_kyberCallReverts() public {
        // decoding fails inside the mock -> router reports BadCallback, user funds untouched
        bytes memory cd = abi.encodePacked(bytes4(0xe21fd0e9), uint256(1));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), cd)));
        assertEq(A.balanceOf(user), 1e30);
    }

    function test_shortCallbackData() public {
        (bool ok, bytes memory e) = address(r).call(abi.encodePacked(bytes4(0xfa461e33), uint256(1)));
        assertFalse(ok);
        assertEq(bytes4(e), TickingRouter.BadCallback.selector);
    }

    function test_zeroOutputIntermediateHop() public {
        fermi.setMode(4, address(0)); // sends nothing
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(
            bytes.concat(
                _h(A, C, 0),
                P.fermi(0, 1000, address(B), address(fermi)),
                P.v3(CH | T, 0, address(C), 0, 100, address(pBC))
            )
        );
    }

    // ───────────── every venue on mocks (exact formula, zero residue) ─────────────
    function test_allVenuesMock() public {
        uint256 e = 997;
        assertEq(_run(bytes.concat(_h(A, B, 0), P.v3ts(T, 1000, address(B), 0, 60, address(pTs)))), e);
        assertEq(_run(bytes.concat(_h(A, B, 0), P.alg(T, 1000, address(B), address(pAlg)))), e);
        assertEq(_run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0)))), e);
        assertEq(
            _run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, bytes32(uint256(0x10000)), address(0)))), e
        );
        assertEq(_run(bytes.concat(_h(A, B, 0), P.bal(T, 1000, address(B), address(0x9001)))), e);
        assertEq(_run(bytes.concat(_h(A, B, 0), P.woo(T, 1000, address(B)))), e);
        assertEq(_run(bytes.concat(_h(A, B, 0), P.tess(T, 1000, address(B)))), e);
        _clean();
        assertEq(A.balanceOf(address(pm)), 2000);
    }

    function test_v3tsAndAlgAuth() public {
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3ts(T, 1000, address(B), 0, 61, address(pTs))));
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3ts(T, 1000, address(B), 2, 60, address(pTs))));
        vm.expectRevert(TickingRouter.BadPool.selector);
        _run(bytes.concat(_h(A, B, 0), P.alg(T, 1000, address(B), address(pAB))));
    }

    function test_v4_dinMismatch() public {
        pm.setMode(1);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0))));
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, bytes32(uint256(0x10000)), address(0))));
    }

    function test_v4_zeroOut() public {
        pm.setMode(2);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0))));
    }

    function test_v4_callbackTwice() public {
        pm.setMode(3);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0))));
    }

    function test_v4_deltaHooksRejected() public {
        // address with AFTER_SWAP_RETURNS_DELTA (1<<2) and BEFORE_SWAP_RETURNS_DELTA (1<<3)
        address[2] memory h = [address(0x4004), address(0x4008)];
        for (uint256 k; k < 2; ++k) {
            vm.expectRevert(TickingRouter.BadPool.selector);
            _run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, h[k])));
        }
        // a non-delta hook (BEFORE_SWAP only, 1<<7) is accepted
        assertEq(_run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0x4080)))), 997);
    }

    function test_pcs_deltaHooksRejected() public {
        bytes32[2] memory pr = [bytes32(uint256(0x10400)), bytes32(uint256(0x10800))];
        for (uint256 k; k < 2; ++k) {
            vm.expectRevert(TickingRouter.BadPool.selector);
            _run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, pr[k], address(0x77))));
        }
        // bitmap is ignored when there is no hook contract
        assertEq(_run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, pr[0], address(0)))), 997);
        // non-delta hook bitmap accepted
        assertEq(
            _run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, bytes32(uint256(0x10040)), address(0x77)))),
            997
        );
    }

    function test_singletonCallbacksUnarmed() public {
        vm.prank(address(pm));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.unlockCallback("");
        vm.prank(address(pm));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.lockAcquired("");
        vm.prank(address(bv));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.balancerCallback(address(0), address(B), rcpt);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        r.lockAcquired("");
    }

    function test_chainedFromRouterAllVenues() public {
        // A -> B (v3, router-held) -> A? not allowed (tout==tokenIn ok, but use C): route A->B->C via each chained venue
        C.mint(address(pm), 1e30);
        C.mint(address(bv), 1e30);
        C.mint(address(woo), 1e30);
        C.mint(address(tess), 1e30);
        bytes memory first = P.v3(0, 10_000, address(B), 0, 100, address(pAB));
        uint256 o1 = uint256(9970) * 997 / 1000;
        assertEq(_run(bytes.concat(_h(A, C, 0), first, P.v4(CH | T, 0, address(C), 500, 10, address(0)))), o1);
        assertEq(
            _run(
                bytes.concat(
                    _h(A, C, 0), first, P.pcs(CH | T, 0, address(C), 500, bytes32(uint256(0x10000)), address(0))
                )
            ),
            o1
        );
        assertEq(_run(bytes.concat(_h(A, C, 0), first, P.bal(CH | T, 0, address(C), address(0x9001)))), o1);
        assertEq(_run(bytes.concat(_h(A, C, 0), first, P.woo(CH | T, 0, address(C)))), o1);
        assertEq(_run(bytes.concat(_h(A, C, 0), first, P.tess(CH | T, 0, address(C)))), o1);
        _clean();
    }

    function test_poolReportsZeroOrPositiveOutput() public {
        pAB.setMode(6);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
        pAB.setMode(7);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_priceLimitDirection() public {
        // B -> A (zfo depends on address order); mock enforces the correct limit side like a real pool
        B.mint(user, 1e6);
        vm.prank(user);
        B.approve(address(r), type(uint256).max);
        assertEq(_run(bytes.concat(_h(B, A, 0), P.v3(T, 1000, address(A), 0, 100, address(pAB)))), 997);
        assertEq(_run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB)))), 997);
    }

    function test_singletonNoCallback() public {
        pm.setMode(4);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v4(T, 1000, address(B), 500, 10, address(0))));
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.pcs(T, 1000, address(B), 500, bytes32(uint256(0x10000)), address(0))));
        bv.setSilent(true);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.bal(T, 1000, address(B), address(0x9001))));
    }

    function test_deadlineBoundaryInclusive() public {
        bytes memory prog = bytes.concat(
            P.header(address(A), address(B), rcpt, 0, uint32(block.timestamp)),
            P.v3(T, 1000, address(B), 0, 100, address(pAB))
        );
        assertEq(_run(prog), 997);
    }

    function test_lockReleasedSameFrame() public {
        DoubleSwapper ds = new DoubleSwapper();
        A.mint(address(ds), 2000);
        ds.go(
            address(r),
            address(A),
            bytes.concat(
                P.header(address(A), address(B), rcpt, 0, uint32(block.timestamp)),
                P.v3(T, 1000, address(B), 0, 100, address(pAB))
            )
        );
        assertEq(B.balanceOf(rcpt), 1994);
    }

    function test_unknownCallbackSelectorFromPool() public {
        pAB.setSel(0xdeadbeef);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_crossVenueCallbackConfusion() public {
        // armed for pAB, genuine PoolManager calls unlockCallback -> must revert
        pAB.setPoker(address(pm));
        pAB.setMode(8);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
        // armed for PM, a stranger calls unlockCallback while V4 hop... (pool==expected but sender wrong)
        EvilCaller e = new EvilCaller();
        pAB.setPoker(address(e));
        pAB.setMode(9);
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_kyberSelectorOnlyPayload() public {
        // n == 4 is well-formed (selector only); the Kyber call itself fails -> BadCallback, not Malformed
        vm.expectRevert(TickingRouter.BadCallback.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), abi.encodePacked(bytes4(0xe21fd0e9)))));
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), hex"e21fd0")));
    }

    function test_lockReleasedWithinSameTx() public {
        // transient lock must be cleared at the end of swap: a 2nd swap in the same tx must work
        bytes memory prog = bytes.concat(_h(A, B, 0), P.v3(T, 1000, address(B), 0, 100, address(pAB)));
        vm.startPrank(user);
        r.swap(prog);
        r.swap(prog);
        vm.stopPrank();
    }

    function test_kyberReturnsTokenInRejected() public {
        // Kyber calldata that sends tokenIn back to the router would strand it: must revert
        bytes memory cd =
            abi.encodePacked(bytes4(0xe21fd0e9), abi.encode(address(A), address(A), 1000, 1000, address(r), false));
        A.mint(address(kyb), 1e6);
        vm.expectRevert(TickingRouter.PartialFill.selector);
        _run(bytes.concat(_h(A, B, 0), P.kyb(T, 1000, address(B), cd)));
    }

    // ───────────── fuzz ─────────────
    /// @dev random amounts + random split ratio across two pools, plus optional hop: exact output, zero residue.
    function testFuzz_split(uint128 amt, uint16 ratioBps, bool hop) public {
        amt = uint128(bound(amt, 2, 1e27));
        uint128 a1 = uint128(uint256(amt) * bound(ratioBps, 1, 9999) / 10_000);
        if (a1 == 0) a1 = 1;
        uint128 a2 = amt - a1;
        vm.assume(a2 > 0);
        uint256 exp = uint256(a1) * 997 / 1000 + uint256(a2) * 997 / 1000;
        vm.assume(uint256(a1) * 997 / 1000 > 0 && uint256(a2) * 997 / 1000 > 0);
        bytes memory prog;
        if (!hop) {
            prog = bytes.concat(
                _h(A, B, uint128(exp)),
                P.v3(T, a1, address(B), 0, 100, address(pAB)),
                P.v3(T, a2, address(B), 0, 500, address(pAB2))
            );
        } else {
            vm.assume((uint256(a1) * 997 / 1000) * 997 / 1000 > 0 && (uint256(a2) * 997 / 1000) * 997 / 1000 > 0);
            exp = (uint256(a1) * 997 / 1000) * 997 / 1000 + (uint256(a2) * 997 / 1000) * 997 / 1000;
            prog = bytes.concat(
                _h(A, C, uint128(exp)),
                P.v3(0, a1, address(B), 0, 100, address(pAB)),
                P.v3(CH | T, 0, address(C), 0, 100, address(pBC)),
                P.v3(0, a2, address(B), 0, 500, address(pAB2)),
                P.v3(CH | T, 0, address(C), 0, 100, address(pBC))
            );
        }
        assertEq(_run(prog), exp);
        assertEq(A.balanceOf(user), 1e30 - amt);
        _clean();
    }

    /// @dev Decoder fuzz: arbitrary bytes from an attacker either revert or leave victim + router untouched.
    function testFuzz_decoder(bytes memory body, uint8 op, uint128 amt) public {
        bytes memory prog = bytes.concat(_h(A, B, 0), abi.encodePacked(op, amt), body);
        address attacker = address(0xBAD);
        vm.prank(attacker);
        try r.swap(prog) {} catch {}
        assertEq(A.balanceOf(victim), 1e30);
        assertEq(A.balanceOf(user), 1e30);
        _clean();
    }
}
