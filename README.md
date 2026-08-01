# 🛡️ VPN Bot Infrastructure

A complete, production-ready open-source infrastructure for deploying a Telegram-based VPN provider. Built to help users bypass internet censorship, this project provides a fully automated pipeline for VPN provisioning, billing, and AI-assisted tech support.

## ✨ Features

- **Telegram Bot Interface**: Fully automated bot for users to manage their VPN subscriptions.
- **Web Store (Loonapie)**: A web-based storefront for purchasing subscriptions outside of Telegram.
- **Billing Integration**: Supports YooKassa, Robokassa, and Telegram Native Payments out of the box.
- **AI Support Assistant (RAG)**: Integrates DeepSeek/OpenAI to automatically answer user questions, reducing the need for human tech support. The AI is context-aware and knows the user's active subscriptions and balances.
- **Automated Provisioning**: Integrates natively with Marzban panel and MTProto proxies for instantaneous credential delivery.
- **Referral & Partner Systems**: Built-in affiliate programs with commission payouts to drive growth.
- **Multi-tier Tariffs**: Support for various subscription tiers (Lite, Pro, Maximum) and custom bandwidth limits.

## 💳 Advanced Payment Integrations
A core feature of this architecture is its highly reliable and flexible billing engine:
- **YooKassa:** Full support for processing CIS bank cards and alternative payment methods via the robust YooKassa API.
- **Robokassa:** Integrated gateway for global payments.
- **Cryptomus (Crypto):** Support for cryptocurrency payments to ensure global accessibility.
- **Telegram Native Payments:** Direct in-app purchases using Telegram's native payment provider.
- **Subscriptions & Referrals:** Explicit renew/upgrade flows and affiliate payout tracking. Recurring charges are disabled by default.

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+
- SQLite
- A running instance of [Marzban](https://github.com/Gozargah/Marzban) for backend VPN management.

### 2. Installation

Clone the repository:
```bash
git clone https://github.com/murlockwarlock/vpnproxybot.git
cd vpnproxybot
```

Install dependencies:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration
Copy the example environment files and fill them out with your credentials:
```bash
cp .env.example .env
```
Edit `.env` to add your Telegram Bot Token, Payment Provider Tokens, and Marzban API credentials.

### 4. Running the Bot
```bash
python -m bot
```

### 5. Running the Web Store
```bash
python -m webstore
```

## 🛠 Deployment

Keep production addresses, paths and credentials in local environment files or your secret manager. They are intentionally not included in this public repository.

## 🤝 Contributing
Contributions are welcome! Please open an issue or submit a pull request if you'd like to improve the codebase.

## 📄 License
This project is open-source and available under the MIT License.
