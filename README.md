# NFT Ethereum Server

Hệ thống mint NFT ERC-721 hoàn chỉnh: Upload ảnh → Hash SHA-256 → Pin IPFS → Mint lên Sepolia.

```
nft-ethereum-server/
├── contracts/
│   └── ImageNFT.sol          ← Solidity ERC-721 contract
├── scripts/
│   └── deploy.js             ← Hardhat deploy script
├── hardhat.config.js
├── backend/
│   ├── app.py                ← Flask server (Bước 3)
│   ├── core.py               ← Core logic: hash + IPFS (Bước 2)
│   ├── ImageNFT.abi.json     ← (bạn tự export sau khi compile)
│   ├── requirements.txt
│   └── .env.example          ← Copy → .env rồi điền giá trị thật
└── frontend/
    └── index.html            ← UI tải ảnh + progress + kết quả
```

---

## Bước 1 — Deploy Smart Contract (Sepolia)

### Cách A: Dùng Hardhat (khuyến nghị)

```bash
# 1. Cài Node.js dependencies
npm init -y
npm install --save-dev hardhat @nomicfoundation/hardhat-toolbox \
    @openzeppelin/contracts dotenv

# 2. Tạo file .env ở thư mục gốc (cùng cấp hardhat.config.js)
cp backend/.env.example .env
# → Điền PRIVATE_KEY và SEPOLIA_RPC_URL

# 3. Compile
npx hardhat compile

# 4. Deploy lên Sepolia
npx hardhat run scripts/deploy.js --network sepolia
# → In ra địa chỉ contract, lưu vào deployment.json

# 5. (Tuỳ chọn) Verify trên Etherscan
npx hardhat verify --network sepolia <CONTRACT_ADDRESS>
```

### Cách B: Dùng Remix IDE (nhanh hơn để test)

1. Mở https://remix.ethereum.org
2. Tạo file `contracts/ImageNFT.sol` → paste code vào.
3. Cài **OpenZeppelin** qua plugin hoặc import trực tiếp từ npm URL.
4. Compile với Solidity 0.8.20.
5. Chuyển sang tab **Deploy & Run** → chọn **Injected Provider - MetaMask**.
6. Chắc chắn MetaMask đang ở mạng **Sepolia**. Nhấn **Deploy**.
7. Copy địa chỉ contract vừa deploy vào file `.env`.
8. Copy ABI (nút **ABI** trong tab Compiler) → lưu thành `backend/ImageNFT.abi.json`.

---

## Bước 2 — Chuẩn bị Backend

```bash
cd backend

# Tạo virtual environment
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate

# Cài dependencies
pip install -r requirements.txt

# Cấu hình môi trường
cp .env.example .env
# Mở .env và điền đầy đủ:
#   PRIVATE_KEY        → Private key ví Ethereum (không có 0x)
#   SEPOLIA_RPC_URL    → Infura / Alchemy RPC URL
#   CONTRACT_ADDRESS   → Địa chỉ contract đã deploy
#   PINATA_JWT         → JWT từ https://app.pinata.cloud/keys

# Đặt file ABI
# Sau khi compile Hardhat: artifacts/contracts/ImageNFT.sol/ImageNFT.json
# Lấy trường "abi" và lưu thành: backend/ImageNFT.abi.json
```

---

## Bước 3 — Chạy Flask Server

```bash
cd backend
source venv/bin/activate

# Development
python app.py

# Production (khuyến nghị)
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

Server chạy tại `http://localhost:5000`.

---

## Bước 4 — Chạy Frontend

Dùng bất kỳ static file server nào:

```bash
# Cách 1: Python built-in
cd frontend
python -m http.server 5500

# Cách 2: VS Code Live Server (cổng 5500 mặc định)
# Cách 3: npx serve frontend -p 5500
```

Mở `http://localhost:5500` trên trình duyệt.

---

## API Endpoint

### `POST /mint`

**Content-Type:** `multipart/form-data`

| Field       | Type   | Bắt buộc | Mô tả                              |
|-------------|--------|----------|------------------------------------|
| `image`     | file   | ✅        | File ảnh (JPG/PNG/GIF/WebP, ≤5MB) |
| `recipient` | string | ✅        | Địa chỉ Ethereum nhận NFT          |

**Response thành công (200):**
```json
{
  "success": true,
  "tx_hash": "0xabc123...",
  "etherscan_url": "https://sepolia.etherscan.io/tx/0xabc123...",
  "image_hash": "e3b0c44298fc1c...",
  "ipfs_image": "ipfs://bafybeig...",
  "token_uri": "ipfs://bafybeih...",
  "recipient": "0xYourAddress..."
}
```

---

## Bảo mật — Tóm tắt các biện pháp đã áp dụng

| Lớp bảo vệ | Chi tiết |
|---|---|
| **Private Key** | Đọc từ `.env`, không bao giờ hardcode trong source |
| **API Keys** | Pinata JWT + RPC URL đều trong `.env` |
| **File extension** | Whitelist: `.jpg .jpeg .png .gif .webp` |
| **Magic bytes** | Server kiểm tra header binary của file, bất chấp MIME type client gửi lên |
| **File size** | Giới hạn 5 MB cả phía Flask lẫn logic `core.py` |
| **CORS** | Chỉ chấp nhận origin được cấu hình trong `ALLOWED_ORIGIN` |
| **Path traversal** | Tên file được sanitize, bỏ directory component |
| **XSS** | Frontend escape tất cả dữ liệu từ API trước khi render HTML |
| **Hash deduplication** | Contract từ chối mint nếu SHA-256 đã tồn tại trên chain |

---

## Lấy ETH Testnet (Faucet)

- https://sepoliafaucet.com
- https://faucet.quicknode.com/ethereum/sepolia
- https://www.alchemy.com/faucets/ethereum-sepolia
