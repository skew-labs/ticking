// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {TickingRouter} from "../src/TickingRouter.sol";
import {Program as P} from "./utils/Program.sol";
import {Base} from "./Unit.t.sol";
import {MockToken, MockWETH, NoEth, MockV3Pool} from "./utils/Mocks.sol";

/// @notice v2: protocol fee (I7) and native-ETH V4 hops (I8).
contract V2Test is Base {
    uint8 constant T = 0x40;
    uint8 constant CH = 0x80;
    uint8 constant NAT = 0x20;

    event Swap(
        address indexed payer,
        address indexed recipient,
        address indexed tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 fee
    );

    function setUp() public override {
        super.setUp();
        vm.deal(user, 1e30);
        vm.prank(user);
        weth.deposit{value: 1e29}();
        vm.prank(user);
        weth.approve(address(r), type(uint256).max);
        vm.deal(address(pm), 1e29); // PoolManager ETH reserves for native take
        B.mint(address(pm), 1e30);
        C.mint(address(pm), 1e30);
    }

    function _hw(address tin, address tout, uint128 minOut) internal view returns (bytes memory) {
        return P.header(tin, tout, rcpt, minOut, uint32(block.timestamp + 60));
    }

    function _ethClean() internal view {
        assertEq(address(r).balance, 0, "router ETH");
        assertEq(weth.balanceOf(address(r)), 0, "router WETH");
        _clean();
    }

    // ───────────── fee (I7) ─────────────

    function test_fee_exactSplit_andEvent() public {
        uint256 g = uint256(1_000_000) * 997 / 1000;
        uint256 fee = g * 5 / 10_000;
        vm.expectEmit(true, true, true, true, address(r));
        emit Swap(user, rcpt, address(A), address(B), 1_000_000, g - fee, fee);
        uint256 out = _run(bytes.concat(_h(A, B, 0), P.v3(T, 1_000_000, address(B), 0, 100, address(pAB))));
        assertEq(out, g - fee);
        assertEq(B.balanceOf(rcpt), g - fee);
        assertEq(B.balanceOf(FEE_TO), fee);
        assertEq(fee, 498);
        _clean();
    }

    function testFuzz_fee_conservation_and_cap(uint128 amt) public {
        amt = uint128(bound(amt, 1001, 1e27));
        uint256 g = uint256(amt) * 997 / 1000;
        uint256 out = _run(bytes.concat(_h(A, B, 0), P.v3(T, amt, address(B), 0, 100, address(pAB))));
        uint256 fee = B.balanceOf(FEE_TO);
        assertEq(out + fee, g, "recipient + feeRecipient == gross");
        assertLe(fee * 10_000, g * 5, "fee <= 5 bps of gross");
        assertEq(fee, g * 5 / 10_000, "floor rounding (user favored)");
        assertEq(B.balanceOf(rcpt), out);
        _clean(); // no dust
    }

    function test_fee_tinyRoundsToZero() public {
        uint256 out = _run(bytes.concat(_h(A, B, 0), P.v3(T, 2000, address(B), 0, 100, address(pAB))));
        assertEq(out, 1994); // floor(1994*5/1e4) = 0
        assertEq(B.balanceOf(FEE_TO), 0);
    }

    function test_minOut_isNet() public {
        uint256 g = uint256(1_000_000) * 997 / 1000;
        uint256 net = g - g * 5 / 10_000;
        bytes memory body = P.v3(T, 1_000_000, address(B), 0, 100, address(pAB));
        vm.expectRevert(abi.encodeWithSelector(TickingRouter.Slippage.selector, net, net + 1));
        _run(bytes.concat(_h(A, B, uint128(net + 1)), body));
        assertEq(_run(bytes.concat(_h(A, B, uint128(net)), body)), net);
    }

    function test_fee_routerPreBalanceNotPaidOut() public {
        B.mint(address(r), 12345); // donation must neither be paid out nor counted as gross
        uint256 out = _run(bytes.concat(_h(A, B, 0), P.v3(T, 1_000_000, address(B), 0, 100, address(pAB))));
        assertEq(out, _net(997_000));
        assertEq(B.balanceOf(address(r)), 12345);
    }

    function test_fee_recipientEqualsFeeRecipient() public {
        bytes memory prog = bytes.concat(
            P.header(address(A), address(B), FEE_TO, 0, uint32(block.timestamp + 60)),
            P.v3(T, 1_000_000, address(B), 0, 100, address(pAB))
        );
        vm.prank(user);
        uint256 out = r.swap(prog);
        assertEq(out, 997_000); // recipient == FEE_RECIPIENT: both legs land there, delta == gross
        assertEq(B.balanceOf(FEE_TO), 997_000);
    }

    function test_constants() public view {
        assertEq(r.FEE_BPS(), 5);
        assertEq(r.FEE_RECIPIENT(), FEE_TO);
        assertEq(r.WETH(), address(weth));
    }

    // ───────────── native ETH V4 (I8) ─────────────

    function test_native_in_WETH_to_B() public {
        uint256 g = uint256(1e18) * 997 / 1000;
        uint256 out =
            _run(bytes.concat(_hw(address(weth), address(B), 0), P.v4(T | NAT, 1e18, address(B), 500, 10, address(0))));
        assertEq(out, _net(g));
        assertEq(B.balanceOf(FEE_TO), g * 5 / 10_000);
        assertEq(pm.ethSettled(), 1e18, "settled exactly a in ETH");
        _ethClean();
    }

    function test_native_out_B_to_WETH() public {
        B.mint(user, 1e30);
        vm.prank(user);
        B.approve(address(r), type(uint256).max);
        uint256 g = uint256(1e18) * 997 / 1000;
        uint256 out = _run(
            bytes.concat(_hw(address(B), address(weth), 0), P.v4(T | NAT, 1e18, address(weth), 500, 10, address(0)))
        );
        assertEq(out, _net(g));
        assertEq(weth.balanceOf(rcpt), _net(g), "recipient gets WETH, never raw ETH");
        assertEq(weth.balanceOf(FEE_TO), g * 5 / 10_000, "fee paid in WETH");
        _ethClean();
    }

    function test_native_out_feeRecipientRejectsEth() public {
        NoEth bad = new NoEth();
        TickingRouter r2 = _router(address(bad));
        B.mint(user, 1e30);
        vm.startPrank(user);
        B.approve(address(r2), type(uint256).max);
        uint256 out = r2.swap(
            bytes.concat(_hw(address(B), address(weth), 0), P.v4(T | NAT, 1e18, address(weth), 500, 10, address(0)))
        );
        vm.stopPrank();
        uint256 g = uint256(1e18) * 997 / 1000;
        assertEq(out, _net(g));
        assertEq(weth.balanceOf(address(bad)), g * 5 / 10_000, "ETH-rejecting fee recipient still paid (WETH)");
        assertEq(address(r2).balance, 0);
    }

    function test_native_out_recipientRejectsEth() public {
        NoEth bad = new NoEth();
        B.mint(user, 1e30);
        vm.prank(user);
        B.approve(address(r), type(uint256).max);
        vm.prank(user);
        uint256 out = r.swap(
            bytes.concat(
                P.header(address(B), address(weth), address(bad), 0, uint32(block.timestamp + 60)),
                P.v4(T | NAT, 1e18, address(weth), 500, 10, address(0))
            )
        );
        assertEq(weth.balanceOf(address(bad)), out);
        _ethClean();
    }

    function test_native_chained_A_WETH_B() public {
        // A -> WETH (v3 mock pool A/WETH) -> B (native V4)
        MockToken W = MockToken(address(weth));
        address pAW = address(_poolW());
        uint256 o1 = uint256(1e18) * 997 / 1000;
        uint256 g = o1 * 997 / 1000;
        uint256 out = _run(
            bytes.concat(
                _h(A, B, 0),
                P.v3(0, 1e18, address(W), 0, 100, pAW),
                P.v4(CH | T | NAT, 0, address(B), 500, 10, address(0))
            )
        );
        assertEq(out, _net(g));
        _ethClean();
    }

    function test_native_forcedEthDoesNotBrick() public {
        vm.deal(address(r), 7 ether); // simulates selfdestruct-forced ETH
        uint256 out =
            _run(bytes.concat(_hw(address(weth), address(B), 0), P.v4(T | NAT, 1e18, address(B), 500, 10, address(0))));
        assertEq(out, _net(uint256(1e18) * 997 / 1000));
        assertEq(address(r).balance, 7 ether, "forced ETH untouched, never paid out");
    }

    function test_native_flagOnNonV4_reverts() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T | NAT, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_reservedFlag_reverts() public {
        vm.expectRevert(TickingRouter.Malformed.selector);
        _run(bytes.concat(_h(A, B, 0), P.v3(T | 0x10, 1000, address(B), 0, 100, address(pAB))));
    }

    function test_native_withoutWethLeg_reverts() public {
        vm.expectRevert(TickingRouter.BadToken.selector);
        _run(bytes.concat(_h(A, B, 0), P.v4(T | NAT, 1000, address(B), 500, 10, address(0))));
    }

    function test_receive_rejectsOutsideSwap() public {
        vm.deal(address(this), 1 ether);
        (bool ok,) = address(r).call{value: 1}("");
        assertFalse(ok, "plain ETH send rejected");
        vm.deal(address(weth), 1 ether);
        vm.prank(address(weth));
        (ok,) = address(r).call{value: 1}("");
        assertFalse(ok, "even WETH cannot push ETH outside a swap");
    }

    function testFuzz_native_roundtrip(uint96 amt, bool dirIn) public {
        amt = uint96(bound(amt, 2001, 1e27));
        uint256 g = uint256(amt) * 997 / 1000;
        uint256 out;
        if (dirIn) {
            out = _run(
                bytes.concat(_hw(address(weth), address(B), 0), P.v4(T | NAT, amt, address(B), 500, 10, address(0)))
            );
            assertEq(B.balanceOf(FEE_TO) + out, g);
        } else {
            B.mint(user, amt);
            vm.prank(user);
            B.approve(address(r), type(uint256).max);
            out = _run(
                bytes.concat(_hw(address(B), address(weth), 0), P.v4(T | NAT, amt, address(weth), 500, 10, address(0)))
            );
            assertEq(weth.balanceOf(FEE_TO) + out, g);
        }
        assertEq(out, _net(g));
        _ethClean();
    }

    // ───────────── helpers ─────────────
    function _router(address feeTo) internal returns (TickingRouter) {
        address d = address(0xdead);
        return new TickingRouter(
            [address(pm), d, d, d, address(kyb), address(fac), d, d, d, d, d, address(pm), address(pm)],
            address(weth),
            feeTo
        );
    }

    function _poolW() internal returns (address p) {
        // MockV3Pool over (A, WETH): reuse Base._pool by treating WETH as a MockToken-compatible ERC20
        MockV3Pool q = new MockV3Pool(address(A), address(weth));
        fac.set(q.token0(), q.token1(), 100, address(q));
        A.mint(address(q), 1e30);
        p = address(q);
        vm.deal(address(this), 1e30);
        weth.deposit{value: 1e29}();
        weth.transfer(p, 1e29);
    }

    // ───────────── mutation-driven: constructor + dust check ─────────────

    function test_ctor_zeroWeth_reverts() public {
        address d = address(0xBEEF);
        vm.expectRevert(TickingRouter.Malformed.selector);
        new TickingRouter([d, d, d, d, d, d, d, d, d, d, d, d, d], address(0), d);
    }

    function test_ctor_zeroFeeRecipient_reverts() public {
        address d = address(0xBEEF);
        vm.expectRevert(TickingRouter.Malformed.selector);
        new TickingRouter([d, d, d, d, d, d, d, d, d, d, d, d, d], d, address(0));
    }

    function test_ctor_setsWethAndFeeRecipient() public {
        address d = address(0xBEEF);
        TickingRouter x = new TickingRouter([d, d, d, d, d, d, d, d, d, d, d, d, d], address(0xE7), address(0xFE));
        assertEq(x.WETH(), address(0xE7));
        assertEq(x.FEE_RECIPIENT(), address(0xFE));
    }

    /// Output token whose balance drifts on transfer out of the router: the payout would leave dust -> BadToken.
    function test_dustLeftInRouter_reverts() public {
        B.setSticky(address(r));
        vm.prank(user);
        vm.expectRevert(TickingRouter.BadToken.selector);
        r.swap(bytes.concat(_h(A, B, 0), P.v3(T, 1_000_000, address(B), 0, 100, address(pAB))));
    }
}
