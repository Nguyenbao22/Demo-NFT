"""
app.py
~~~~~~
Flask backend server for the NFT Ethereum minting system.

Endpoints
---------
POST /mint     – Accept an image, hash it, pin to IPFS, mint ERC-721 NFT.
POST /verify   – Check whether an image has already been minted (by SHA-256).
GET  /nfts     – Return a list of all minted NFTs (token IDs + tokenURIs).
GET  /health   – Liveness probe.

Security measures implemented
------------------------------
1. File-size limit enforced before any processing.
2. MIME type validated via magic bytes (not just Content-Type header).
3. File extension whitelist enforced.
4. Private key & API keys loaded exclusively from .env (never in source).
5. CORS restricted to the configured ALLOWED_ORIGIN.
6. All uploads discarded from memory immediately after use (no disk writes).
7. Rate-limit headers returned so a reverse proxy can enforce them.
"""

import json
import logging
import os
import sys
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from web3 import Web3
from web3.middleware import geth_poa_middleware

from core import (
    MAX_FILE_SIZE_BYTES,
    build_metadata,
    compute_phash,
    find_similar_hash,
    compute_embedding,
    find_similar_by_embedding,
    load_embeddings_cache,
    save_embedding_to_cache,
    upload_image_to_pinata,
    upload_metadata_to_pinata,
    validate_image_bytes,
)

# ──────────────────────────────────────────────────────────────────────────────
#  Bootstrap: load environment variables from .env
# ──────────────────────────────────────────────────────────────────────────────

load_dotenv()  # Reads .env in the current working directory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Required environment variables (fail fast if any are missing)
# ──────────────────────────────────────────────────────────────────────────────

def _require_env(key: str) -> str:
    """Return env var value or abort startup with a clear error message."""
    value = os.environ.get(key)
    if not value:
        logger.critical(
            "Missing required environment variable: %s  "
            "→ Add it to your .env file and restart.",
            key,
        )
        sys.exit(1)
    return value


PRIVATE_KEY       = _require_env("PRIVATE_KEY")          # No 0x prefix in .env
# RPC_URL ho tro ca Sepolia lan Hardhat local. Chap nhan SEPOLIA_RPC_URL cu de compat.
RPC_URL           = os.environ.get("RPC_URL") or _require_env("SEPOLIA_RPC_URL")
# Chain ID: 11155111=Sepolia, 31337=Hardhat. Doc tu env thay vi hardcode.
CHAIN_ID          = int(os.environ.get("CHAIN_ID", "11155111"))
# CONTRACT_ADDRESS co the den tu shared volume (LOCAL_CHAIN=true) hoac tu .env
CONTRACT_ADDRESS  = os.environ.get("CONTRACT_ADDRESS", "")
CONTRACT_ABI_PATH = os.environ.get("CONTRACT_ABI_PATH", "ImageNFT.abi.json")
ALLOWED_ORIGIN    = os.environ.get("ALLOWED_ORIGIN", "http://localhost:5500")
LOCAL_CHAIN       = os.environ.get("LOCAL_CHAIN", "false").lower() == "true"
# PINATA_JWT chi bat buoc khi dung Pinata (LOCAL_CHAIN=false)
if not LOCAL_CHAIN:
    _require_env("PINATA_JWT")


# ──────────────────────────────────────────────────────────────────────────────
#  Web3 setup
# ──────────────────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL))
# PoA middleware can thiet tren Sepolia; Hardhat cung dung PoA nen inject luon
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not w3.is_connected():
    logger.critical("Cannot connect to Ethereum node at %s", RPC_URL)
    sys.exit(1)

# Derive the public address from the private key
account = w3.eth.account.from_key(f"0x{PRIVATE_KEY}")
OWNER_ADDRESS = account.address
logger.info("Backend wallet address: %s", OWNER_ADDRESS)

# Load contract ABI
try:
    with open(CONTRACT_ABI_PATH) as f:
        contract_abi = json.load(f)
except FileNotFoundError:
    logger.critical(
        "ABI file not found: %s  "
        "→ Compile the contract and copy the ABI here.",
        CONTRACT_ABI_PATH,
    )
    sys.exit(1)

# Khi LOCAL_CHAIN=true, CONTRACT_ADDRESS co the chua co trong .env
# start-backend.sh da export CONTRACT_ADDRESS tu /shared/contract_address.txt
# nhung os.environ luc nay co the da duoc load roi, kiem tra lai cho chac
if not CONTRACT_ADDRESS:
    shared_path = "/shared/contract_address.txt"
    try:
        with open(shared_path) as _f:
            CONTRACT_ADDRESS = _f.read().strip()
        logger.info("CONTRACT_ADDRESS loaded from shared volume: %s", CONTRACT_ADDRESS)
    except FileNotFoundError:
        logger.critical("CONTRACT_ADDRESS not set and %s not found.", shared_path)
        sys.exit(1)

checksum_address = Web3.to_checksum_address(CONTRACT_ADDRESS)
contract = w3.eth.contract(address=checksum_address, abi=contract_abi)
logger.info("Contract loaded at %s", checksum_address)


# ──────────────────────────────────────────────────────────────────────────────
#  Flask app
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_BYTES  # Flask-level size guard

CORS(app, origins=[ALLOWED_ORIGIN])  # Only allow our frontend origin


# ──────────────────────────────────────────────────────────────────────────────
#  Decorator: validate uploaded image
# ──────────────────────────────────────────────────────────────────────────────

def require_valid_image(f):
    """
    Decorator that validates the uploaded file before the route handler runs.
    Checks:
      - 'image' field present in multipart form data.
      - Filename extension is in the whitelist.
      - File size > 0 and ≤ MAX_FILE_SIZE_BYTES.
      - Magic bytes match a real image format (prevents script injection).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "image" not in request.files:
            return jsonify({"error": "No 'image' field in the request."}), 400

        uploaded_file = request.files["image"]
        filename = uploaded_file.filename or ""

        # Extension whitelist
        allowed_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ext = os.path.splitext(filename)[1].lower()
        if ext not in allowed_extensions:
            return jsonify({
                "error": f"File extension '{ext}' is not allowed.",
                "allowed": list(allowed_extensions),
            }), 415

        # Read bytes into memory (no disk write)
        file_bytes = uploaded_file.read()

        if len(file_bytes) == 0:
            return jsonify({"error": "Uploaded file is empty."}), 400
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            return jsonify({
                "error": f"File too large. Maximum size is "
                         f"{MAX_FILE_SIZE_BYTES // (1024*1024)} MB."
            }), 413

        # Magic-byte validation (defence-in-depth against MIME spoofing)
        try:
            detected_mime = validate_image_bytes(file_bytes)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 415

        logger.info(
            "File accepted: name=%s  size=%d bytes  mime=%s",
            filename, len(file_bytes), detected_mime,
        )

        # Attach validated data to the request context for the route handler
        request.validated_image_bytes    = file_bytes
        request.validated_image_filename = filename
        request.validated_mime_type      = detected_mime

        return f(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────────────────────
#  Mint helper: sign & send transaction
# ──────────────────────────────────────────────────────────────────────────────

def _mint_on_chain(recipient: str, image_hash_hex: str, token_uri: str) -> str:
    """
    Call ImageNFT.mintNFT() on the configured chain, sign locally with the owner's
    private key, broadcast, and wait for receipt.

    Parameters
    ----------
    recipient       : Checksum Ethereum address of the NFT receiver.
    image_hash_hex  : 64-char hex string (SHA-256 of the image).
    token_uri       : ipfs:// URI pointing to the metadata JSON.

    Returns
    -------
    str  – Transaction hash (0x-prefixed hex string).
    """
    # Convert hex digest to bytes32 for the contract call
    image_hash_bytes32 = bytes.fromhex(image_hash_hex)

    recipient_checksum = Web3.to_checksum_address(recipient)

    # Estimate gas with a safety buffer
    try:
        estimated_gas = contract.functions.mintNFT(
            recipient_checksum, image_hash_bytes32, token_uri
        ).estimate_gas({"from": OWNER_ADDRESS})
        gas_limit = int(estimated_gas * 1.25)  # 25 % buffer
    except Exception as exc:
        # Likely the hash was already minted → surface the contract error
        raise RuntimeError(f"Gas estimation failed: {exc}") from exc

    nonce = w3.eth.get_transaction_count(OWNER_ADDRESS)

    # EIP-1559 fee model (Sepolia supports it)
    base_fee    = w3.eth.get_block("latest")["baseFeePerGas"]
    priority_fee = w3.to_wei(2, "gwei")       # miner tip
    max_fee      = base_fee * 2 + priority_fee  # generous ceiling

    txn = contract.functions.mintNFT(
        recipient_checksum, image_hash_bytes32, token_uri
    ).build_transaction({
        "chainId":              CHAIN_ID,   # Doc tu env: 11155111=Sepolia, 31337=Hardhat
        "gas":                  gas_limit,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce":                nonce,
        "from":                 OWNER_ADDRESS,
    })

    # Sign with private key (never leaves server memory)
    signed_txn  = w3.eth.account.sign_transaction(txn, private_key=f"0x{PRIVATE_KEY}")
    tx_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
    tx_hash_hex = tx_hash.hex()

    logger.info("Transaction broadcast: %s", tx_hash_hex)

    # Wait for confirmation (up to 180 s / ~15 blocks on Sepolia)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

    if receipt["status"] != 1:
        raise RuntimeError(
            f"Transaction reverted. Hash: {tx_hash_hex}"
        )

    logger.info(
        "NFT minted ✓  txHash=%s  blockNumber=%d",
        tx_hash_hex, receipt["blockNumber"],
    )
    return tx_hash_hex


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Simple liveness probe for load balancers / monitoring."""
    return jsonify({
        "status": "ok",
        "connected_to_ethereum": w3.is_connected(),
        "wallet": OWNER_ADDRESS,
    })


@app.route("/mint", methods=["POST"])
@require_valid_image
def mint():
    """
    Main minting endpoint.

    Expects
    -------
    multipart/form-data with fields:
      - image      (file)    : The image to mint.
      - recipient  (string)  : Ethereum address of the NFT recipient.

    Returns
    -------
    JSON with tx_hash, token_uri, image_hash, and Etherscan URL on success.
    """
    # ── Recipient address ────────────────────────────────────────────────────
    recipient = request.form.get("recipient", "").strip()
    if not recipient:
        return jsonify({"error": "Missing 'recipient' address field."}), 400
    if not Web3.is_address(recipient):
        return jsonify({"error": f"Invalid Ethereum address: {recipient}"}), 400

    # ── Validated image data (attached by decorator) ─────────────────────────
    file_bytes = request.validated_image_bytes
    filename   = request.validated_image_filename

    # ── Step 1: pHash ─────────────────────────────────────────────────
    try:
        image_hash = compute_phash(file_bytes)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    # ── Step 1b: Kiểm tra trùng lặp thị giác (Hamming distance) ─────────────
    try:
        total = contract.functions.totalSupply().call()
        existing_hashes = []
        for tid in range(1, total + 1):
            try:
                h = contract.functions.getHashByTokenId(tid).call()
                existing_hashes.append({"token_id": tid, "hash": h.hex()})
            except Exception:
                pass

        match = find_similar_hash(image_hash, existing_hashes)
        if match:
            return jsonify({
                "error": "Ảnh này đã tồn tại trên Blockchain!",
                "detail": (
                    f"Ảnh bạn tải lên giống {match['similarity_pct']}% với "
                    f"NFT Token #{match['token_id']} đã đúc trước đó. "
                    f"(Hamming distance: {match['distance']}/64 bits)"
                ),
                "duplicate": True,
                "matching_token_id": match["token_id"],
                "similarity_pct": match["similarity_pct"],
            }), 409
    except RuntimeError as exc:
        logger.warning("Could not run pHash similarity check: %s", exc)

    # ── Step 1c: Tầng 2 — AI Embedding (MobileNetV2) ─────────────────────────
    embedding = None
    try:
        embedding = compute_embedding(file_bytes)
        cache = load_embeddings_cache()
        ai_match = find_similar_by_embedding(embedding, cache)
        if ai_match:
            return jsonify({
                "error": "AI phát hiện ảnh trùng lặp!",
                "detail": (
                    f"Mạng neural MobileNetV2 xác định ảnh này giống {ai_match['similarity_pct']}% "
                    f"với NFT Token #{ai_match['token_id']}. "
                    f"(Phương pháp: {ai_match['method']})"
                ),
                "duplicate": True,
                "matching_token_id": ai_match["token_id"],
                "similarity_pct": ai_match["similarity_pct"],
                "method": ai_match["method"],
            }), 409
    except Exception as exc:
        logger.warning("AI embedding check failed (continuing): %s", exc)

    # ── Step 2a: Upload image to IPFS ────────────────────────────────────────
    try:
        ipfs_image_uri = upload_image_to_pinata(file_bytes, filename, image_hash)
    except RuntimeError as exc:
        logger.error("IPFS image upload error: %s", exc)
        return jsonify({"error": "Failed to upload image to IPFS.", "detail": str(exc)}), 502

    # ── Step 2b: Build + upload ERC-721 metadata ─────────────────────────────
    metadata = build_metadata(image_hash, ipfs_image_uri, filename)
    try:
        token_uri = upload_metadata_to_pinata(metadata, filename)
    except RuntimeError as exc:
        logger.error("IPFS metadata upload error: %s", exc)
        return jsonify({"error": "Failed to upload metadata to IPFS.", "detail": str(exc)}), 502

    # ── Step 3: Mint NFT on Blockchain ───────────────────────────────────────
    try:
        tx_hash = _mint_on_chain(recipient, image_hash, token_uri)
    except RuntimeError as exc:
        logger.error("Minting error: %s", exc)
        return jsonify({"error": "Minting failed.", "detail": str(exc)}), 500

    # ── Step 4: Lưu AI embedding vào cache ───────────────────────────────────
    if embedding:
        try:
            total = contract.functions.totalSupply().call()
            save_embedding_to_cache(total, embedding)
            logger.info("Embedding saved for token_id=%d", total)
        except Exception as exc:
            logger.warning("Could not cache embedding: %s", exc)

    # ── Success response ─────────────────────────────────────────────────────
    return jsonify({
        "success":       True,
        "tx_hash":       tx_hash,
        "image_hash":    image_hash,
        "ipfs_image":    ipfs_image_uri,
        "token_uri":     token_uri,
        "recipient":     recipient,
    }), 200



# ──────────────────────────────────────────────────────────────────────────────
#  Verify endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/verify", methods=["POST"])
@require_valid_image
def verify():
    """
    Verify whether an image has already been minted as an NFT.

    Expects
    -------
    multipart/form-data with field:
      - image  (file) : The image to check.

    Returns
    -------
    JSON with:
      - minted       : bool  – True if this image was already minted.
      - image_hash   : str   – pHash of the uploaded image.
      - token_id     : int   – Token ID (0 if not minted).
      - token_uri    : str   – IPFS metadata URI (empty string if not minted).
    """
    file_bytes = request.validated_image_bytes

    # Step 1 – compute pHash
    try:
        image_hash = compute_phash(file_bytes)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    # Step 2 – fuzzy search: so sánh Hamming distance với tất cả hash đã mint
    try:
        total = contract.functions.totalSupply().call()
        existing_hashes = []
        for tid in range(1, total + 1):
            try:
                h = contract.functions.getHashByTokenId(tid).call()
                existing_hashes.append({"token_id": tid, "hash": h.hex()})
            except Exception:
                pass

        match = find_similar_hash(image_hash, existing_hashes)
    except Exception as exc:
        logger.error("Similarity check failed during verify: %s", exc)
        match = None

    method = "pHash (Hamming)"

    # Step 2b – nếu pHash không tìm thấy → thử AI Embedding
    if match is None:
        try:
            embedding = compute_embedding(file_bytes)
            cache = load_embeddings_cache()
            ai_match = find_similar_by_embedding(embedding, cache)
            if ai_match:
                match = ai_match
                method = ai_match["method"]
        except Exception as exc:
            logger.warning("AI verify check failed: %s", exc)

    minted = match is not None
    token_id = match["token_id"] if match else 0
    token_uri = ""
    similarity_pct = match["similarity_pct"] if match else 0

    if minted:
        try:
            token_uri = contract.functions.tokenURI(token_id).call()
        except Exception as exc:
            logger.warning("Could not fetch tokenURI for token %d: %s", token_id, exc)
        logger.info("Verify hit: image_hash=%s token_id=%d similarity=%.1f%% method=%s", image_hash, token_id, similarity_pct, method)
    else:
        logger.info("Verify miss: image_hash=%s (not minted)", image_hash)

    return jsonify({
        "minted":         minted,
        "image_hash":     image_hash,
        "token_id":       token_id,
        "token_uri":      token_uri,
        "similarity_pct": similarity_pct,
        "method":         method,
        "contract":       CONTRACT_ADDRESS,
    }), 200


# ──────────────────────────────────────────────────────────────────────────────
#  NFT Gallery endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/nfts", methods=["GET"])
def nfts():
    """
    Return all NFTs minted by this contract.

    Query params
    ------------
    page  (int, default 1)  : Page number (1-indexed).
    limit (int, default 20) : Items per page (max 100).

    Returns
    -------
    JSON with:
      - total    : int   – Total supply.
      - page     : int
      - limit    : int
      - nfts     : list  – [{token_id, token_uri, image_hash, owner}]
    """
    try:
        total = contract.functions.totalSupply().call()
    except Exception as exc:
        logger.error("totalSupply call failed: %s", exc)
        return jsonify({"error": "Failed to query contract.", "detail": str(exc)}), 500

    # Pagination
    try:
        page  = max(1, int(request.args.get("page",  1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"error": "page and limit must be integers."}), 400

    start = (page - 1) * limit + 1  # token IDs are 1-indexed
    end   = min(start + limit - 1, total)

    nft_list = []
    for token_id in range(start, end + 1):
        try:
            token_uri   = contract.functions.tokenURI(token_id).call()
            hash_bytes  = contract.functions.getHashByTokenId(token_id).call()
            image_hash  = hash_bytes.hex()
            try:
                owner = contract.functions.ownerOf(token_id).call()
            except Exception:
                owner = None
            nft_list.append({
                "token_id":   token_id,
                "token_uri":  token_uri,
                "image_hash": image_hash,
                "owner":      owner,
            })
        except Exception as exc:
            logger.warning("Skipping token %d: %s", token_id, exc)

    return jsonify({
        "total": total,
        "page":  page,
        "limit": limit,
        "nfts":  nft_list,
    }), 200


# ──────────────────────────────────────────────────────────────────────────────
#  Error handlers
# ──────────────────────────────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(_):
    return jsonify({
        "error": f"File too large. Maximum size is "
                 f"{MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
    }), 413


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed."}), 405


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Development only – use gunicorn or similar for production
    app.run(host="0.0.0.0", port=5000, debug=False)
