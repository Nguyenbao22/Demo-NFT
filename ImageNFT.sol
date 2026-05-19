// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/Counters.sol";

/**
 * @title ImageNFT
 * @dev ERC-721 contract for minting NFTs from images with SHA-256 hash deduplication.
 *      Only the contract owner (deployer / backend server wallet) can mint.
 */
contract ImageNFT is ERC721URIStorage, Ownable {
    using Counters for Counters.Counter;

    // ─────────────────────────────────────────────
    //  State Variables
    // ─────────────────────────────────────────────

    Counters.Counter private _tokenIdCounter;

    /// @dev Maps SHA-256 image hash → tokenId (1-indexed).
    ///      If the value is 0, the hash has not been minted yet.
    mapping(bytes32 => uint256) private _hashToTokenId;

    /// @dev Maps tokenId → SHA-256 image hash for reverse lookup.
    mapping(uint256 => bytes32) private _tokenIdToHash;

    // ─────────────────────────────────────────────
    //  Events
    // ─────────────────────────────────────────────

    event NFTMinted(
        address indexed recipient,
        uint256 indexed tokenId,
        bytes32 imageHash,
        string ipfsURI
    );

    // ─────────────────────────────────────────────
    //  Custom Errors  (gas-efficient vs. require strings)
    // ─────────────────────────────────────────────

    error HashAlreadyMinted(bytes32 imageHash, uint256 existingTokenId);
    error InvalidRecipient();
    error EmptyTokenURI();

    // ─────────────────────────────────────────────
    //  Constructor
    // ─────────────────────────────────────────────

    constructor() ERC721("ImageNFT", "INFT") Ownable(msg.sender) {}

    // ─────────────────────────────────────────────
    //  External / Public Functions
    // ─────────────────────────────────────────────

    /**
     * @notice Mint a new NFT for the given image.
     * @dev    Callable only by the contract owner (your backend wallet).
     *         Reverts if the same SHA-256 hash has already been minted.
     *
     * @param recipient  Wallet address that will receive the NFT.
     * @param imageHash  SHA-256 hash of the image file (bytes32).
     * @param tokenURI_  IPFS URI pointing to the NFT metadata JSON.
     * @return tokenId   The newly minted token ID.
     */
    function mintNFT(
        address recipient,
        bytes32 imageHash,
        string calldata tokenURI_
    ) external onlyOwner returns (uint256 tokenId) {
        // ── Validations ──────────────────────────
        if (recipient == address(0)) revert InvalidRecipient();
        if (bytes(tokenURI_).length == 0) revert EmptyTokenURI();

        uint256 existing = _hashToTokenId[imageHash];
        if (existing != 0) revert HashAlreadyMinted(imageHash, existing);

        // ── Mint ─────────────────────────────────
        _tokenIdCounter.increment();
        tokenId = _tokenIdCounter.current();

        _safeMint(recipient, tokenId);
        _setTokenURI(tokenId, tokenURI_);

        // ── Record hash ──────────────────────────
        _hashToTokenId[imageHash] = tokenId;
        _tokenIdToHash[tokenId]   = imageHash;

        emit NFTMinted(recipient, tokenId, imageHash, tokenURI_);
    }

    // ─────────────────────────────────────────────
    //  View / Pure Functions
    // ─────────────────────────────────────────────

    /// @notice Returns the tokenId minted for a given image hash (0 = not minted).
    function getTokenIdByHash(bytes32 imageHash)
        external
        view
        returns (uint256)
    {
        return _hashToTokenId[imageHash];
    }

    /// @notice Returns the image hash stored for a given tokenId.
    function getHashByTokenId(uint256 tokenId)
        external
        view
        returns (bytes32)
    {
        return _tokenIdToHash[tokenId];
    }

    /// @notice Returns the total number of NFTs minted so far.
    function totalSupply() external view returns (uint256) {
        return _tokenIdCounter.current();
    }

    /// @notice Returns true if an image with this hash has already been minted.
    function isHashMinted(bytes32 imageHash) external view returns (bool) {
        return _hashToTokenId[imageHash] != 0;
    }
}
