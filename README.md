# Store-Ai-Agent

A full-featured, autonomous AI agent for Shopify stores with an embeddable chat widget, multi-tenant backend, and automated action execution.

Built with **FastAPI**, **Shopify Admin & Storefront API**, **Appwrite**, and **Langfuse**, designed for seamless deployment on **Render**.

---

## 🚀 Key Agent Capabilities

1. **Order History & Retrieval**
   - Displays all active, past, fulfilled, and unfulfilled orders.
   - Works automatically for authenticated/logged-in shoppers and via email verification for guests.

2. **Live Shipment & Order Tracking**
   - Real-time order tracking with carrier status, checkpoint locations, and estimated delivery dates.
   - Interactive order status cards rendered directly within the chat widget.

3. **Warranty Claims & Replacements**
   - File claims with auto-generated unique Claim IDs (`CLM-<order_number>-<hash>`).
   - Automatically tags orders and appends staff notes in Shopify Admin API.
   - Claim status lookups by Claim ID or Order Number.

4. **Store Policies Check**
   - Queries official store policies (Refund, Return, Shipping, Warranty, Privacy, Terms of Service, Cancellation) directly from Shopify with fallback answers.

5. **Direct Natural Language Cart Updates**
   - Add items, remove items, change quantities, and clear cart directly from user messages.
   - Synchronizes backend Storefront API with browser theme AJAX cart (`/cart/add.js`) in real time.

6. **Payment Credentials & Autonomous OTP Payment**
   - Masked card storage (`•••• •••• •••• 4242`).
   - 6-digit OTP challenge generated and asked in the chat dialog.
   - On OTP verification, creates a live confirmed order in Shopify Admin API (`financial_status: "paid"`).

---

## 🛠️ Deploying on Render

### 1. Web Service Configuration
- **Environment**: `Python 3`
- **Build Command**:
  ```bash
  pip install -r requirements.txt
  ```
- **Start Command**:
  ```bash
  uvicorn main:app --host 0.0.0.0 --port $PORT
  ```

### 2. Environment Variables

| Variable | Description |
| :--- | :--- |
| `PRIMARY_LLM_API_KEY` | Your primary OpenAI/OpenAI-compatible LLM API Key |
| `PRIMARY_LLM_BASE_URL` | Base URL for LLM provider (optional for standard OpenAI) |
| `PRIMARY_LLM_MODEL` | Model name (e.g., `gpt-4o-mini`, `Qwen/Qwen3-8B-AWQ`) |
| `SHOPIFY_API_KEY` | Shopify App Client ID |
| `SHOPIFY_API_SECRET` | Shopify App Client Secret |
| `SHOPIFY_SCOPES` | `read_products,write_products,read_orders,write_orders,read_customers` |
| `APPWRITE_ENDPOINT` | Appwrite Endpoint URL (e.g. `https://cloud.appwrite.io/v1`) |
| `APPWRITE_PROJECT_ID` | Appwrite Project ID |
| `APPWRITE_API_KEY` | Appwrite server API key with databases.* scopes |
| `APPWRITE_DATABASE_ID` | Appwrite Database ID |
| `SESSION_SECRET` | Strong random secret key for session cookies |
| `AFTERSHIP_API_KEY` | *(Optional)* For live package tracking checkpoints |

---

## 🛍️ Embedding the Chat Widget on Shopify

Add this single script tag to your Shopify theme (`theme.liquid` before `</body>`):

```html
<script src="https://<your-render-app-domain>/widget.js"
        data-shop="your-store.myshopify.com"
        defer></script>
```

---

## 🧪 Testing Locally

Run automated tests:
```bash
python test_suite.py
```

Run local development server:
```bash
uvicorn main:app --reload --port 8000
```
