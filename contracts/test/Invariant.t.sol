// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Test.sol";
import {TickingRouter} from "../src/TickingRouter.sol";
import {Program as P} from "./utils/Program.sol";
import {Base} from "./Unit.t.sol";
import {MockToken, MockV3Pool, MockFermi} from "./utils/Mocks.sol";

contract Handler is Test {
    TickingRouter r;
    MockToken A;
    MockToken B;
    MockToken C;
    address pAB;
    address pAB2;
    address pBC;
    address fermi;
    address public victim;
    uint256 public ok;
    uint256 public calls;
    uint256 public minOutViolations;

    constructor(
        TickingRouter r_,
        MockToken a,
        MockToken b,
        MockToken c,
        address x,
        address y,
        address z,
        address f,
        address v
    ) {
        (r, A, B, C, pAB, pAB2, pBC, fermi, victim) = (r_, a, b, c, x, y, z, f, v);
    }

    /// @dev Well-formed but randomly parameterized route from a random actor; may also be corrupted.
    function route(uint256 seed, uint128 amt, uint16 ratio, uint128 minOut, uint8 corrupt) external {
        calls++;
        address actor = address(uint160(bound(seed, 1, 3)) + 0x1000);
        amt = uint128(bound(amt, 2, 1e24));
        uint128 a1 = uint128(bound(uint256(amt) * ratio / 65535, 1, amt - 1));
        A.mint(actor, amt);
        vm.prank(actor);
        A.approve(address(r), amt);
        address rcpt = address(0xCAFE);
        bytes memory prog = bytes.concat(
            P.header(address(A), address(C), rcpt, minOut, uint32(block.timestamp + 1)),
            P.v3(0, a1, address(B), 0, 100, pAB),
            P.v3(0xC0, 0, address(C), 0, 100, pBC),
            P.fermi(0, amt - a1, address(B), fermi),
            P.v3(0xC0, 0, address(C), 0, 100, pBC)
        );
        if (corrupt % 4 == 0) prog[bound(seed, 80, prog.length - 1)] ^= bytes1(corrupt);
        uint256 before = C.balanceOf(rcpt);
        vm.prank(actor);
        try r.swap(prog) returns (uint256 out) {
            ok++;
            if (C.balanceOf(rcpt) - before != out || out < minOut) minOutViolations++;
        } catch {}
    }
}

contract InvariantTest is Base {
    Handler h;

    function setUp() public override {
        super.setUp();
        MockFermi f = new MockFermi(address(A), address(B));
        B.mint(address(f), 1e30);
        fermi = f;
        h = new Handler(r, A, B, C, address(pAB), address(pAB2), address(pBC), address(f), victim);
        targetContract(address(h));
    }

    /// I2: router never retains any token or allowance.
    function invariant_routerHoldsNothing() public view {
        _clean();
    }

    /// I1: a third party with a max approval is never touched.
    function invariant_victimUntouched() public view {
        assertEq(A.balanceOf(victim), 1e30);
    }

    /// I3: every successful call delivered >= minOut, measured at the recipient.
    function invariant_minOut() public view {
        assertEq(h.minOutViolations(), 0);
    }
}
