// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {MockToken} from "./Mocks.sol";

/// @notice Kyber-like router mock. Calldata after the allowed selector: (tin, tout, amountIn, out, to, pullLess).
contract MockKyber {
    fallback() external {
        (address tin, address tout, uint256 a, uint256 out, address to, bool less) =
            abi.decode(msg.data[4:], (address, address, uint256, uint256, address, bool));
        MockToken(tin).transferFrom(msg.sender, address(this), less ? a - 1 : a);
        MockToken(tout).transfer(to, out);
    }
}
