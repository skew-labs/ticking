// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Test-side encoder for the packed route program (mirror of ARCHITECTURE.md §Encoding).
library Program {
    uint8 constant CHAIN = 0x80;
    uint8 constant TERM = 0x40;

    function header(address tin, address tout, address rcpt, uint128 minOut, uint32 deadline)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(tin, tout, rcpt, minOut, deadline);
    }

    function _in(uint8 op, uint128 amt) private pure returns (bytes memory) {
        return op & CHAIN != 0 ? abi.encodePacked(op) : abi.encodePacked(op, amt);
    }

    function v3(uint8 flags, uint128 amt, address tout, uint8 fid, uint24 fee, address pool)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(_in(1 | flags, amt), tout, fid, fee, pool);
    }

    function v3ts(uint8 flags, uint128 amt, address tout, uint8 fid, int24 ts, address pool)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(_in(2 | flags, amt), tout, fid, uint24(ts), pool);
    }

    function alg(uint8 flags, uint128 amt, address tout, address pool) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(3 | flags, amt), tout, pool);
    }

    function v4(uint8 flags, uint128 amt, address tout, uint24 fee, int24 ts, address hooks)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(_in(4 | flags, amt), tout, fee, uint24(ts), hooks);
    }

    function woo(uint8 flags, uint128 amt, address tout) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(5 | flags, amt), tout);
    }

    function fermi(uint8 flags, uint128 amt, address tout, address pool) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(6 | flags, amt), tout, pool);
    }

    function tess(uint8 flags, uint128 amt, address tout) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(7 | flags, amt), tout);
    }

    function bal(uint8 flags, uint128 amt, address tout, address pool) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(8 | flags, amt), tout, pool);
    }

    function pcs(uint8 flags, uint128 amt, address tout, uint24 fee, bytes32 params, address hooks)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(_in(10 | flags, amt), tout, fee, params, hooks);
    }

    function kyb(uint8 flags, uint128 amt, address tout, bytes memory cd) internal pure returns (bytes memory) {
        return abi.encodePacked(_in(9 | flags, amt), tout, uint16(cd.length), cd);
    }
}
