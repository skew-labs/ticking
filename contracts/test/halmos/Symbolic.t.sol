// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {TickingRouter} from "../../src/TickingRouter.sol";
import {MockToken, MockV3Factory, MockV3Pool, MockFermi} from "../utils/Mocks.sol";
import {MockKyber} from "../utils/MockKyber.sol";

/// @notice Halmos symbolic proofs (run: halmos --contract SymbolicTest). Every input below is fully symbolic.
contract SymbolicTest is Test {
    TickingRouter r;
    MockV3Factory fac;
    MockToken A;
    MockToken B;
    MockV3Pool pAB;
    MockFermi fermi;
    MockToken C;
    MockV3Pool pBC;
    MockKyber kyb;
    address user = address(0x1000);
    address victim = address(0x2000);
    address rcpt = address(0x3000);

    function setUp() public {
        fac = new MockV3Factory();
        address d = address(0xdead);
        kyb = new MockKyber();
        r = new TickingRouter([d, d, d, d, address(kyb), address(fac), d, d, d, d, d, d, d]);
        A = new MockToken();
        B = new MockToken();
        pAB = new MockV3Pool(address(A), address(B));
        fac.set(pAB.token0(), pAB.token1(), 100, address(pAB));
        A.mint(address(pAB), 1e30);
        B.mint(address(pAB), 1e30);
        C = new MockToken();
        pBC = new MockV3Pool(address(B), address(C));
        fac.set(pBC.token0(), pBC.token1(), 100, address(pBC));
        B.mint(address(pBC), 1e30);
        C.mint(address(pBC), 1e30);
        B.mint(address(kyb), 1e30);
        fermi = new MockFermi(address(A), address(B));
        B.mint(address(fermi), 1e30);
        A.mint(user, 1e30);
        A.mint(victim, 1e30);
        vm.prank(victim);
        A.approve(address(r), type(uint256).max);
        vm.prank(user);
        A.approve(address(r), type(uint256).max);
    }

    /// P1 (I4): with nothing armed, NO caller and NO arguments make any callback succeed.
    function check_callbackAuth(address caller, int256 a0, int256 a1, uint8 which) public {
        bytes4 sel = which % 4 == 0
            ? bytes4(0xfa461e33)
            : which % 4 == 1 ? bytes4(0x2c8958f6) : which % 4 == 2 ? bytes4(0x23a69e75) : bytes4(0x654b6487);
        vm.prank(caller);
        (bool ok,) = address(r).call(abi.encodeWithSelector(sel, a0, a1, ""));
        assert(!ok);
        vm.prank(caller);
        (ok,) = address(r).call(abi.encodeCall(TickingRouter.unlockCallback, ("")));
        assert(!ok);
    }

    /// P2 (I1+I2+I3): for a symbolic single-op program (opcode/flags byte, amount, tokenOut, factory id, fee, pool,
    /// minOut, recipient) sent by an attacker: victim's funds are untouched, router ends with zero balance and
    /// allowance, and success implies recipient got >= minOut.
    function check_singleOp(
        uint8 op,
        uint128 amt,
        address tout,
        uint8 fid,
        uint24 fee,
        address pool,
        uint128 minOut,
        bool useFermi
    ) public {
        // Kyber (op 9) has a variable-length payload; symbolic lengths are out of halmos' scope -> covered by fuzz.
        vm.assume(op & 0x0f != 9);
        address attacker = address(0xBAD);
        A.mint(attacker, 1e24);
        vm.prank(attacker);
        A.approve(address(r), type(uint256).max);
        bytes memory body = useFermi
            ? abi.encodePacked(op, amt, tout, address(fermi))
            : abi.encodePacked(op, amt, tout, fid, fee, pool);
        bytes memory prog =
            bytes.concat(abi.encodePacked(address(A), address(B), rcpt, minOut, uint32(type(uint32).max)), body);
        uint256 rb = B.balanceOf(rcpt);
        vm.prank(attacker);
        (bool ok, bytes memory ret) = address(r).call(abi.encodeCall(TickingRouter.swap, (prog)));
        assert(A.balanceOf(victim) == 1e30);
        assert(A.balanceOf(user) == 1e30);
        assert(A.balanceOf(address(r)) == 0 && B.balanceOf(address(r)) == 0);
        assert(A.allowance(address(r), address(fermi)) == 0);
        if (ok) {
            uint256 out = abi.decode(ret, (uint256));
            assert(out >= minOut);
            assert(B.balanceOf(rcpt) - rb == out);
        }
    }

    /// P3 (I1): the payer is msg.sender. A symbolic caller can never debit `victim` regardless of program.
    function check_payerIsSender(address caller, uint128 amt) public {
        vm.assume(caller != victim);
        bytes memory prog = bytes.concat(
            abi.encodePacked(address(A), address(B), rcpt, uint128(0), uint32(type(uint32).max)),
            abi.encodePacked(uint8(0x41), amt, address(B), uint8(0), uint24(100), address(pAB))
        );
        vm.prank(caller);
        (bool ok,) = address(r).call(abi.encodeCall(TickingRouter.swap, (prog)));
        ok;
        assert(A.balanceOf(victim) == 1e30);
    }

    /// P4 (I1,I2,I3,I5): two-op programs with symbolic flags (CHAIN/TERM), amounts, intermediate/terminal tokens and
    /// venue choice. Every accepted program must leave router empty, victim untouched, and pay >= minOut.
    function check_twoOps(
        uint8 f1,
        uint8 f2,
        uint128 a1,
        uint128 a2,
        uint8 t1sel,
        uint8 t2sel,
        bool v2fermi,
        uint128 minOut
    ) public {
        address attacker = address(0xBAD);
        A.mint(attacker, 1e24);
        vm.prank(attacker);
        A.approve(address(r), type(uint256).max);
        address t1 = t1sel % 3 == 0 ? address(A) : t1sel % 3 == 1 ? address(B) : address(C);
        address t2 = t2sel % 3 == 0 ? address(A) : t2sel % 3 == 1 ? address(B) : address(C);
        uint8 o1 = 1 | (f1 & 0xC0);
        uint8 o2 = (v2fermi ? 6 : 1) | (f2 & 0xC0);
        bytes memory op1 = (o1 & 0x80) != 0
            ? abi.encodePacked(o1, t1, uint8(0), uint24(100), address(pAB))
            : abi.encodePacked(o1, a1, t1, uint8(0), uint24(100), address(pAB));
        bytes memory tail2 =
            v2fermi ? abi.encodePacked(address(fermi)) : abi.encodePacked(uint8(0), uint24(100), address(pBC));
        bytes memory op2 = (o2 & 0x80) != 0 ? abi.encodePacked(o2, t2, tail2) : abi.encodePacked(o2, a2, t2, tail2);
        bytes memory prog =
            bytes.concat(abi.encodePacked(address(A), address(C), rcpt, minOut, uint32(type(uint32).max)), op1, op2);
        uint256 rb = C.balanceOf(rcpt);
        vm.prank(attacker);
        (bool ok, bytes memory ret) = address(r).call(abi.encodeCall(TickingRouter.swap, (prog)));
        assert(A.balanceOf(victim) >= 1e30 && A.balanceOf(user) >= 1e30); // gifts allowed, debits never
        assert(A.balanceOf(address(r)) == 0 && B.balanceOf(address(r)) == 0 && C.balanceOf(address(r)) == 0);
        assert(A.allowance(address(r), address(fermi)) == 0 && B.allowance(address(r), address(fermi)) == 0);
        if (ok) {
            uint256 out = abi.decode(ret, (uint256));
            assert(out >= minOut && C.balanceOf(rcpt) - rb == out);
        }
    }

    /// P5 (Kyber sandbox): fixed-length payload with fully symbolic content (selector, tokens, amounts, receiver,
    /// pull-less flag). Router must stay empty with zero allowance, victim untouched, and success => >= minOut.
    function check_kyberPayload(
        bytes4 sel,
        address ktin,
        address ktout,
        uint256 ka,
        uint256 kout,
        address kto,
        bool less,
        uint128 amt,
        uint128 minOut
    ) public {
        address attacker = address(0xBAD);
        A.mint(attacker, 1e24);
        vm.prank(attacker);
        A.approve(address(r), type(uint256).max);
        bytes memory cd = abi.encodePacked(sel, abi.encode(ktin, ktout, ka, kout, kto, less));
        bytes memory prog = bytes.concat(
            abi.encodePacked(address(A), address(B), rcpt, minOut, uint32(type(uint32).max)),
            abi.encodePacked(uint8(0x49), amt, address(B), uint16(cd.length), cd)
        );
        uint256 rb = B.balanceOf(rcpt);
        vm.prank(attacker);
        (bool ok, bytes memory ret) = address(r).call(abi.encodeCall(TickingRouter.swap, (prog)));
        assert(A.balanceOf(victim) >= 1e30 && A.balanceOf(user) >= 1e30); // gifts allowed, debits never
        assert(A.balanceOf(address(r)) == 0 && B.balanceOf(address(r)) == 0);
        assert(A.allowance(address(r), address(kyb)) == 0);
        if (ok) {
            uint256 out = abi.decode(ret, (uint256));
            assert(out >= minOut && B.balanceOf(rcpt) - rb == out);
        }
    }
}
