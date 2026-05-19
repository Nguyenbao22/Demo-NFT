// scripts/deploy.js
// Run: npx hardhat run scripts/deploy.js --network sepolia

const { ethers } = require("hardhat");

async function main() {
  console.log("🚀 Deploying ImageNFT to Sepolia testnet...");

  const [deployer] = await ethers.getSigners();
  console.log(`📋 Deployer address : ${deployer.address}`);
  console.log(
    `💰 Deployer balance : ${ethers.formatEther(
      await ethers.provider.getBalance(deployer.address)
    )} ETH`
  );

  const ImageNFT = await ethers.getContractFactory("ImageNFT");
  const contract = await ImageNFT.deploy();
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  console.log(`✅ ImageNFT deployed at: ${address}`);
  console.log(
    `🔗 Etherscan: https://sepolia.etherscan.io/address/${address}`
  );

  // ── Write address to a JSON file so the backend can read it ──
  const fs = require("fs");
  const deploymentInfo = {
    network: "sepolia",
    contractAddress: address,
    deployedAt: new Date().toISOString(),
    deployer: deployer.address,
  };
  fs.writeFileSync(
    "deployment.json",
    JSON.stringify(deploymentInfo, null, 2)
  );
  console.log("📝 Deployment info saved to deployment.json");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
