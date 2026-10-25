// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import "forge-std/Script.sol";
import {TickingRouter} from "../src/TickingRouter.sol";

/// @notice RH mainnet (4663) deployment. Reads the key ONLY from env DEPLOYER_PK; never logs it.
///         Dry run:  forge script script/Deploy.s.sol --rpc-url $RH_RPC
///         Broadcast (only with explicit user go): add --broadcast
contract Deploy is Script {
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant CRCL = 0xdF0992E440dD0be65BD8439b609d6D4366bf1CB5;
    address constant T117 = 0x117cc2133c37B721F49dE2A7a74833232B3B4C0C;

    function addrs() public pure returns (address[13] memory a) {
        a = [
            0x8366a39CC670B4001A1121B8F6A443A643e40951, // 0 Uniswap V4 PoolManager
            0xB055000fbE3cc9bDE7742C583a86cc6283E3fF85, // 1 Balancer V3 vault
            0x5520385bFcf07Ec87C4c53A7d8d65595Dff69FA4, // 2 WooPP
            0x55555522005BcAE1c2424D474BfD5ed477749E3e, // 3 Tessera
            0x6131B5fae19EA4f9D964eAc0408E4408b66337b5, // 4 Kyber MetaAggregationRouterV2
            0x1f7d7550B1b028f7571E69A784071F0205FD2EfA, // 5 Uniswap V3 factory   (fid 0, fee)
            0xE51960f1B45f1C9FB6D166E6a884F866fC70433B, // 6 Sushi V3 factory     (fid 1, fee)
            0xEce6eCd61177336ea6Fb9b17937AC439D85EE20B, // 7 V3 fork factory      (fid 2, fee)
            0xE0c4ceb92d08CA985bB70fe0a22fEb121A9854A8, // 8 V3 tickSpacing fork  (fid 0, ts)
            0x1ac9dB4a2608ba45D6127B1737949b51Bb54B7F3, // 9 V3 tickSpacing fork  (fid 1, ts)
            0x16494A80E08Bcb9285D87b67149d7b01774D82F8, // 10 Algebra (Alandale) factory
            0x4F922d5B15e6691e0469663E4F5C4177f23c5FaF, // 11 Pancake Infinity vault
            0xeE04c68742e6Bf434bE8039580D2e89BBE55bc6f // 12 Pancake Infinity CLPoolManager
        ];
    }

    /// @dev Every constructor arg is checked against live on-chain behavior before deploying.
    function verifyArgs(address[13] memory a) public view {
        require(block.chainid == 4663, "not Robinhood Chain mainnet");
        for (uint256 i; i < 13; ++i) {
            require(a[i].code.length > 0, string.concat("no code at arg ", vm.toString(i)));
        }
        require(_v3(a[5], 100) == 0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca, "uni factory");
        require(_v3(a[6], 500) == 0x9B050cb1F265094b0977160a2BD7dA8C9E529c3C, "sushi factory");
        require(_v3(a[7], 100) == 0xb2a6aD51b3ea3CDc8D3508Cca147A43471382e53, "fork factory");
        require(_ts(a[8], T117, USDG, 1) == 0x38453c115607463Ac284820Ce959831042f3Df4E, "ts factory 0");
        require(_ts(a[9], USDG, CRCL, 60) == 0xbE1563DA6CC4E3057334Fd10735beBc6770B537F, "ts factory 1");
        (, bytes memory r) = a[10].staticcall(abi.encodeWithSignature("poolByPair(address,address)", T117, USDG));
        require(abi.decode(r, (address)) == 0x05E146F995aA70d0BF8481Ee3D27368A537a8e0D, "algebra factory");
        (, r) = a[12].staticcall(abi.encodeWithSignature("vault()"));
        require(abi.decode(r, (address)) == a[11], "pcs vault/clpm mismatch");
        (, r) = a[1].staticcall(
            abi.encodeWithSignature("getPoolTokens(address)", 0xc2fEf42121FaDA751ba9f2107C4c8d4C593af435)
        );
        require(abi.decode(r, (address[])).length == 2, "balancer vault");
    }

    function _v3(address f, uint24 fee) internal view returns (address p) {
        (, bytes memory r) = f.staticcall(abi.encodeWithSignature("getPool(address,address,uint24)", WETH, USDG, fee));
        p = abi.decode(r, (address));
    }

    function _ts(address f, address x, address y, int24 ts) internal view returns (address p) {
        (, bytes memory r) = f.staticcall(abi.encodeWithSignature("getPool(address,address,int24)", x, y, ts));
        p = abi.decode(r, (address));
    }

    function run() external returns (TickingRouter router) {
        address[13] memory a = addrs();
        verifyArgs(a);
        uint256 pk = vm.envUint("DEPLOYER_PK"); // never logged
        vm.startBroadcast(pk);
        address weth = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73; // canonical RH WETH
        address feeTo = vm.envAddress("FEE_RECIPIENT"); // 0x3e13cd3704099424a6301Ea4125791a6a1433637 (user-confirmed EOA)
        require(feeTo.code.length == 0 && feeTo != address(0), "fee recipient must be an EOA");
        router = new TickingRouter(a, weth, feeTo);
        vm.stopBroadcast();
        // post-deploy immutables check
        require(router.POOL_MANAGER() == a[0] && router.KYBER() == a[4] && router.F_ALG() == a[10], "immutables");
        require(router.PCS_VAULT() == a[11] && router.PCS_CLPM() == a[12], "immutables pcs");
        require(router.WETH() == weth && router.FEE_RECIPIENT() == feeTo && router.FEE_BPS() == 5, "fee immutables");
        console.log("TickingRouter deployed at", address(router));
        console.log("constructor args (abi):");
        console.logBytes(abi.encode(a));
    }
}
