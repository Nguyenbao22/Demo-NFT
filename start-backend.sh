#!/bin/sh
set -e

# If LOCAL_CHAIN is set, wait for private blockchain and read contract address
if [ "$LOCAL_CHAIN" = "true" ]; then
  echo "🔗 Mode: PRIVATE BLOCKCHAIN"
  echo "Waiting for Hardhat node at $SEPOLIA_RPC_URL ..."

  # Node is already checked by Docker healthcheck
  echo "✅ Blockchain node should be reachable!"

  # Wait for contract deployment (shared volume)
  echo "Waiting for contract deployment..."
  until [ -f /shared/contract_address.txt ]; do
    echo "  ... contract not deployed yet, retrying in 3s"
    sleep 3
  done

  # Read contract address from shared volume
  export CONTRACT_ADDRESS=$(cat /shared/contract_address.txt)
  echo "✅ Contract loaded: $CONTRACT_ADDRESS"
fi

echo "🚀 Starting Flask backend (gunicorn)..."
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
