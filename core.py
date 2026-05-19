"""
core.py
~~~~~~~
Core logic module for the NFT minting pipeline.

Functions
---------
compute_sha256(file_bytes)   → str  (hex digest)
upload_to_pinata(file_bytes, filename, image_hash) → str  (ipfs:// URI)
build_metadata(image_hash, ipfs_image_uri, filename) → dict
upload_metadata_to_pinata(metadata, token_name)     → str  (ipfs:// URI)
"""

import hashlib
import json
import logging
import os
from typing import Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────────────

PINATA_API_URL_FILE    = "https://api.pinata.cloud/pinning/pinFileToIPFS"
PINATA_API_URL_JSON    = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
PINATA_GATEWAY_PREFIX  = "https://gateway.pinata.cloud/ipfs/"

# IPFS node rieng (Kubo) - dung khi LOCAL_CHAIN=true
# IPFS_API_URL doc tu env, default tro vao service "ipfs" trong docker-compose
IPFS_API_URL = os.environ.get("IPFS_API_URL", "http://ipfs:5001")
LOCAL_CHAIN  = os.environ.get("LOCAL_CHAIN", "false").lower() == "true"

# Max file size accepted by this backend (5 MB)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

# Whitelisted MIME types (validated server-side; frontend check is advisory only)
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ──────────────────────────────────────────────────────────────────────────────
#  AI Embedding (MobileNetV2) — Tầng 2: phát hiện crop/lật/filter
# ──────────────────────────────────────────────────────────────────────────────

import numpy as np

AI_SIMILARITY_THRESHOLD = 0.70   # Cosine similarity >= 0.70 → coi là cùng 1 ảnh
EMBEDDINGS_CACHE_PATH = "/shared/embeddings_cache.json"

# Singleton model (lazy-loaded)
_mobilenet_model = None
_mobilenet_transform = None


def _get_mobilenet():
    """Lazy-load MobileNetV2 model (singleton, chỉ load 1 lần)."""
    global _mobilenet_model, _mobilenet_transform
    if _mobilenet_model is None:
        import torch
        import torchvision.models as models
        import torchvision.transforms as transforms

        logger.info("Loading MobileNetV2 model...")
        _mobilenet_model = models.mobilenet_v2(weights='DEFAULT')
        _mobilenet_model.classifier = torch.nn.Identity()  # Bỏ classification head
        _mobilenet_model.eval()

        _mobilenet_transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        logger.info("MobileNetV2 loaded successfully.")
    return _mobilenet_model, _mobilenet_transform


def compute_embedding(file_bytes: bytes) -> list:
    """
    Trích xuất vector đặc trưng 1280 chiều từ ảnh bằng MobileNetV2.
    Vector được L2-normalize để cosine similarity hoạt động chính xác.
    """
    import io
    import torch
    from PIL import Image

    model, transform = _get_mobilenet()

    img = Image.open(io.BytesIO(file_bytes)).convert('RGB')
    tensor = transform(img).unsqueeze(0)

    with torch.no_grad():
        embedding = model(tensor).squeeze().numpy()

    # L2 normalize
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    return embedding.tolist()


def cosine_similarity(vec_a: list, vec_b: list) -> float:
    """Tính cosine similarity giữa 2 vector."""
    a = np.array(vec_a)
    b = np.array(vec_b)
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def load_embeddings_cache() -> dict:
    """Load cache embeddings từ shared volume."""
    try:
        with open(EMBEDDINGS_CACHE_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_embedding_to_cache(token_id: int, embedding: list):
    """Lưu embedding mới vào cache."""
    cache = load_embeddings_cache()
    cache[str(token_id)] = embedding
    try:
        with open(EMBEDDINGS_CACHE_PATH, 'w') as f:
            json.dump(cache, f)
    except Exception as exc:
        logger.warning("Could not save embedding cache: %s", exc)


def find_similar_by_embedding(new_embedding: list, cache: dict) -> dict | None:
    """
    So sánh embedding mới với tất cả embeddings đã lưu.
    Trả về thông tin match nếu tìm thấy ảnh tương tự.
    """
    best_match = None
    best_sim = AI_SIMILARITY_THRESHOLD

    for token_id_str, cached_emb in cache.items():
        sim = cosine_similarity(new_embedding, cached_emb)
        logger.info(
            "AI compare: new vs token_id=%s → cosine=%.4f (threshold=%.2f)",
            token_id_str, sim, AI_SIMILARITY_THRESHOLD,
        )
        if sim >= AI_SIMILARITY_THRESHOLD and sim > best_sim:
            best_sim = sim
            best_match = {
                "token_id": int(token_id_str),
                "similarity_pct": round(sim * 100, 1),
                "method": "AI (MobileNetV2)",
            }

    if best_match:
        logger.info(
            "AI similarity match: token_id=%d similarity=%.1f%%",
            best_match["token_id"], best_match["similarity_pct"],
        )
    return best_match

# ──────────────────────────────────────────────────────────────────────────────
#  Helper: get Pinata auth headers from environment
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
#  Upload len IPFS local (Kubo node) — khong can API key
# ──────────────────────────────────────────────────────────────────────────────

def _upload_to_local_ipfs(file_bytes: bytes, filename: str) -> str:
    """
    Upload file len Kubo IPFS node chay local.
    Tra ve ipfs://<CID>.
    """
    try:
        response = requests.post(
            f"{IPFS_API_URL}/api/v0/add",
            files={"file": (filename, file_bytes)},
            params={"cid-version": "1", "pin": "true"},
            timeout=30,
        )
        response.raise_for_status()
    except RequestException as exc:
        logger.error("Local IPFS upload failed: %s", exc)
        raise RuntimeError(f"Local IPFS upload failed: {exc}") from exc

    cid = response.json().get("Hash")
    if not cid:
        raise RuntimeError("IPFS node did not return a CID")

    logger.info("Uploaded to local IPFS: ipfs://%s (%s)", cid, filename)
    return f"ipfs://{cid}"


def _upload_json_to_local_ipfs(data: dict, name: str) -> str:
    """Upload JSON metadata len Kubo IPFS node chay local."""
    json_bytes = json.dumps(data).encode()
    return _upload_to_local_ipfs(json_bytes, f"{name}.json")


def _pinata_headers(json_mode: bool = False) -> dict:
    """
    Build Pinata authorization headers.
    Reads PINATA_JWT from environment (set via .env / python-dotenv in app.py).
    Raises RuntimeError if the key is missing.
    """
    jwt = os.environ.get("PINATA_JWT")
    if not jwt:
        raise RuntimeError(
            "PINATA_JWT environment variable is not set. "
            "Add it to your .env file."
        )
    headers = {"Authorization": f"Bearer {jwt}"}
    if json_mode:
        headers["Content-Type"] = "application/json"
    return headers


# ──────────────────────────────────────────────────────────────────────────────
#  Step 2a – SHA-256 hash
# ──────────────────────────────────────────────────────────────────────────────

def compute_phash(file_bytes: bytes) -> str:
    """
    Compute the pHash (Perceptual Hash) of raw image bytes.

    Parameters
    ----------
    file_bytes : bytes
        Raw contents of the uploaded image file.

    Returns
    -------
    str
        64-character lowercase hex string padded with zeros, e.g.
        "000000000000000000000000000000000000000000000000e4b33b1e95e7c805".
    """
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise TypeError("file_bytes must be a bytes-like object")
    if len(file_bytes) == 0:
        raise ValueError("Cannot hash an empty file")
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"File too large: {len(file_bytes)} bytes "
            f"(max {MAX_FILE_SIZE_BYTES} bytes)"
        )

    import io
    import imagehash
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(file_bytes))
        # Compute pHash (normally a 16 char hex string)
        phash_value = str(imagehash.phash(img))
        # Pad to 64 chars to simulate bytes32 for Ethereum smart contract compatibility
        digest = phash_value.zfill(64)
    except Exception as exc:
        logger.error("pHash computation failed: %s", exc)
        raise ValueError(f"Could not compute pHash for the image: {exc}") from exc

    logger.info("pHash computed: %s (padded: %s)", phash_value, digest)
    return digest


# ──────────────────────────────────────────────────────────────────────────────
#  pHash similarity helpers
# ──────────────────────────────────────────────────────────────────────────────

# Ngưỡng khoảng cách Hamming: ≤ THRESHOLD → coi là "cùng 1 bức ảnh".
# pHash 64-bit → khoảng cách tối đa = 64.
# Facebook/OpenSea thường dùng ngưỡng ~10.
PHASH_SIMILARITY_THRESHOLD = 10


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """
    Compute the Hamming distance (số bit khác nhau) giữa hai mã pHash.

    Parameters
    ----------
    hash_a, hash_b : str
        Chuỗi hex 64-char (đã zero-pad từ compute_phash).

    Returns
    -------
    int  – Số bit khác nhau (0 = giống hệt, 64 = hoàn toàn khác).
    """
    # Chỉ lấy phần pHash thực sự (bỏ padding 0 phía trước)
    a = int(hash_a, 16)
    b = int(hash_b, 16)
    xor = a ^ b
    return bin(xor).count("1")


def find_similar_hash(new_hash: str, existing_hashes: list) -> dict | None:
    """
    Kiểm tra xem new_hash có giống ảnh nào đã tồn tại không.

    Parameters
    ----------
    new_hash        : str   – pHash 64-char hex của ảnh mới.
    existing_hashes : list  – Danh sách dict: [{"token_id": int, "hash": str}, ...]

    Returns
    -------
    dict | None  – Nếu tìm thấy ảnh tương tự, trả về dict chứa thông tin.
                   Nếu không có ảnh nào giống, trả về None.
    """
    best_match = None
    best_distance = PHASH_SIMILARITY_THRESHOLD + 1  # Bắt đầu ngoài ngưỡng

    for item in existing_hashes:
        dist = hamming_distance(new_hash, item["hash"])
        if dist <= PHASH_SIMILARITY_THRESHOLD and dist < best_distance:
            best_distance = dist
            best_match = {
                "token_id": item["token_id"],
                "existing_hash": item["hash"],
                "distance": dist,
                "similarity_pct": round((1 - dist / 64) * 100, 1),
            }

    if best_match:
        logger.info(
            "Similar image found: token_id=%d distance=%d similarity=%.1f%%",
            best_match["token_id"], best_match["distance"], best_match["similarity_pct"],
        )
    return best_match


# ──────────────────────────────────────────────────────────────────────────────
#  Step 2b – Upload image to Pinata / IPFS
# ──────────────────────────────────────────────────────────────────────────────

def upload_image_to_pinata(
    file_bytes: bytes,
    filename: str,
    image_hash: str,
) -> str:
    """
    Upload anh len IPFS.
    - LOCAL_CHAIN=true  → dung Kubo node local (khong can Pinata JWT)
    - LOCAL_CHAIN=false → dung Pinata API (can PINATA_JWT)

    Parameters
    ----------
    file_bytes  : bytes   – Raw image bytes.
    filename    : str     – Original filename.
    image_hash  : str     – SHA-256 hex digest (dung lam metadata tag tren Pinata).

    Returns
    -------
    str – IPFS URI dang ``ipfs://<CID>``
    """
    if LOCAL_CHAIN:
        return _upload_to_local_ipfs(file_bytes, filename)
    _validate_filename(filename)

    # Pinata metadata pinned alongside the file
    pinata_metadata = json.dumps(
        {
            "name": f"NFT Image – {filename}",
            "keyvalues": {"sha256": image_hash},
        }
    )
    pinata_options = json.dumps({"cidVersion": 1})

    # Determine MIME type from extension for the multipart upload
    mime_type = _mime_from_filename(filename)

    files = {
        "file": (filename, file_bytes, mime_type),
        "pinataMetadata": (None, pinata_metadata),
        "pinataOptions": (None, pinata_options),
    }

    try:
        response = requests.post(
            PINATA_API_URL_FILE,
            files=files,
            headers=_pinata_headers(json_mode=False),
            timeout=30,
        )
        response.raise_for_status()
    except RequestException as exc:
        logger.error("Pinata image upload failed: %s", exc)
        raise RuntimeError(f"Pinata image upload failed: {exc}") from exc

    cid = response.json().get("IpfsHash")
    if not cid:
        raise RuntimeError("Pinata response did not contain IpfsHash")

    ipfs_uri = f"ipfs://{cid}"
    logger.info("Image pinned to IPFS: %s", ipfs_uri)
    return ipfs_uri


# ──────────────────────────────────────────────────────────────────────────────
#  Build ERC-721 metadata JSON
# ──────────────────────────────────────────────────────────────────────────────

def build_metadata(
    image_hash: str,
    ipfs_image_uri: str,
    filename: str,
    description: Optional[str] = None,
) -> dict:
    """
    Build an OpenSea-compatible ERC-721 metadata dict.

    Parameters
    ----------
    image_hash      : SHA-256 hex digest of the image.
    ipfs_image_uri  : IPFS URI of the image file  (ipfs://<CID>).
    filename        : Original filename (used as token name).
    description     : Optional human-readable description.

    Returns
    -------
    dict  – Metadata compliant with the ERC-721 Metadata JSON Schema.
    """
    return {
        "name": f"Image NFT – {filename}",
        "description": description or f"On-chain NFT for image '{filename}'.",
        "image": ipfs_image_uri,
        "attributes": [
            {"trait_type": "SHA-256 Hash", "value": image_hash},
            {"trait_type": "Source File",  "value": filename},
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Upload metadata JSON to Pinata / IPFS
# ──────────────────────────────────────────────────────────────────────────────

def upload_metadata_to_pinata(metadata: dict, token_name: str) -> str:
    """
    Upload metadata JSON len IPFS.
    - LOCAL_CHAIN=true  → Kubo local
    - LOCAL_CHAIN=false → Pinata

    Parameters
    ----------
    metadata    : dict – ERC-721 metadata dictionary.
    token_name  : str  – Ten hien thi / ten file.

    Returns
    -------
    str – IPFS URI dang ``ipfs://<CID>``
    """
    if LOCAL_CHAIN:
        return _upload_json_to_local_ipfs(metadata, token_name)
    payload = {
        "pinataContent": metadata,
        "pinataMetadata": {"name": f"NFT Metadata – {token_name}"},
        "pinataOptions": {"cidVersion": 1},
    }

    try:
        response = requests.post(
            PINATA_API_URL_JSON,
            json=payload,
            headers=_pinata_headers(json_mode=True),
            timeout=30,
        )
        response.raise_for_status()
    except RequestException as exc:
        logger.error("Pinata metadata upload failed: %s", exc)
        raise RuntimeError(f"Pinata metadata upload failed: {exc}") from exc

    cid = response.json().get("IpfsHash")
    if not cid:
        raise RuntimeError(
            "Pinata metadata response did not contain IpfsHash"
        )

    ipfs_uri = f"ipfs://{cid}"
    logger.info("Metadata pinned to IPFS: %s", ipfs_uri)
    return ipfs_uri


# ──────────────────────────────────────────────────────────────────────────────
#  Internal security helpers
# ──────────────────────────────────────────────────────────────────────────────

# Extension → MIME type whitelist
_EXT_MIME_MAP = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}

# Allowed "magic bytes" per format for binary content sniffing
# (checked in app.py before this module is called)
IMAGE_MAGIC_BYTES = {
    b"\xff\xd8\xff": "image/jpeg",              # JPEG
    b"\x89PNG\r\n\x1a\n": "image/png",          # PNG
    b"GIF87a": "image/gif",                      # GIF87
    b"GIF89a": "image/gif",                      # GIF89
    b"RIFF": "image/webp",                       # WebP (RIFF…WEBP)
}


def validate_image_bytes(file_bytes: bytes) -> str:
    """
    Validate that the raw bytes belong to a known image format by
    inspecting the magic-byte header.  Returns the detected MIME type.
    Raises ValueError if the bytes don't match any known format.

    NOTE: This is defence-in-depth – a malicious actor who renames a
    script to image.jpg is stopped here regardless of the Content-Type
    header they send.
    """
    for magic, mime in IMAGE_MAGIC_BYTES.items():
        if file_bytes.startswith(magic):
            # Extra WebP check: bytes 8-12 must be b"WEBP"
            if mime == "image/webp" and file_bytes[8:12] != b"WEBP":
                continue
            return mime
    raise ValueError(
        "File content does not match any allowed image format "
        "(JPEG, PNG, GIF, WebP)."
    )


def _validate_filename(filename: str) -> None:
    """Reject filenames with path-traversal or dangerous characters."""
    import os as _os
    # Strip any directory component – only the basename is used
    basename = _os.path.basename(filename)
    if basename != filename:
        raise ValueError("Filename must not contain directory separators.")
    ext = _os.path.splitext(basename)[1].lower()
    if ext not in _EXT_MIME_MAP:
        raise ValueError(
            f"File extension '{ext}' is not allowed. "
            f"Allowed: {list(_EXT_MIME_MAP)}"
        )


def _mime_from_filename(filename: str) -> str:
    import os as _os
    ext = _os.path.splitext(filename)[1].lower()
    return _EXT_MIME_MAP.get(ext, "application/octet-stream")
