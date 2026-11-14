"""
Turns structured action results (order data, product lists, policy text,
etc.) into a natural-sounding reply in the user's own language.

Keeps the two concerns separate on purpose:
  - intent_classifier.py decides WHAT the user wants and detects their language
  - reply_generator.py decides HOW to phrase the answer, in that language

This means your Shopify data layer never needs to know or care about
language at all — it just returns structured data, and this module handles
the human-facing phrasing.

RAG integration: when rag_context is passed in (a list of {"content", "score",
"document"} dicts from rag_retriever.retrieve_context), it's injected into the
prompt as grounding material, and the model is instructed to answer only from
it — this keeps knowledge-base-backed answers factual instead of relying on
the model's own training data.
"""

import os
import json
import re
import llm_selector
try:
    import rag_retriever
except ImportError:
    rag_retriever = None

if (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip() and (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip():
    try:
        from langfuse import observe, get_client
    except ImportError:
        def observe(*args, **kwargs):
            def decorator(f):
                return f
            return decorator
        def get_client(*args, **kwargs):
            return None
else:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    def get_client(*args, **kwargs):
        return None



_client = None

# Qwen3-family models can emit an internal reasoning block wrapped in
# <think>...</think> before the actual answer when "thinking mode" is on.
# Strip it out so only the final, user-facing reply ever reaches the widget.
#  primary model : Qwen/Qwen3-8B-AWQ 
_THINK_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)    # regular expression to handle text in think tag as well as thought tag

def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "pa": "Punjabi",
    "ta": "Tamil",
    "mr": "Marathi",
    "bn": "Bengali",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "de": "German",
    "zh": "Chinese",
    "ja": "Japanese",
    "pt": "Portuguese",
}


def _format_rag_context(rag_context: list[dict]) -> str:
    """Turns retrieved chunks into a numbered block for the prompt."""
    if not rag_context:
        return ""
    parts = []
    for i, chunk in enumerate(rag_context, start=1):
        content = chunk.get("content", "").strip()
        if content:
            parts.append(f"[{i}] {content}")
    return "\n\n".join(parts)

@observe(name="generate_reply")
def generate_reply(
    action_name: str,
    data: dict,
    language: str,
    original_message: str,
    store_identifier=None, needs_rag=False,
    rag_context: list[dict] | None = None,
    rag_query: str | None = None,   # NEW — defaults to original_message if not given

) -> str:
    """
    action_name: e.g. "track_order", "search_products"
    data: whatever your Shopify data layer returned (dict/list/etc.)
    language: ISO 639-1 code from the classifier, e.g. "hi"
    original_message: the user's original message, for tone/context
    rag_context: optional list of retrieved chunks from rag_retriever.retrieve_context(),
                 e.g. [{"content": "...", "score": 0.87, "document": "policy.txt"}, ...]
    """
    if rag_context is None and needs_rag:
        if not store_identifier:
            print("reply_generator: needs_rag=True but no store_identifier given, skipping retrieval")
        elif rag_retriever:
            try:
                rag_context = rag_retriever.retrieve_context(store_identifier, rag_query or original_message)
            except Exception as e:
                print(f"reply_generator: rag retrieval failed: {e!r}")
                rag_context = None
            
    language_name = LANGUAGE_NAMES.get(language, language)

    rag_context_text = _format_rag_context(rag_context) if rag_context else ""
    rag_clause = ""
    if rag_context_text:
        rag_clause = (
            " You have been given reference material retrieved from the store's knowledge base "
            "below. Base your answer ONLY on that material — do not use outside knowledge or make "
            "assumptions beyond what it states. If the material doesn't contain enough information "
            "to answer, say so honestly and suggest the user contact support instead of guessing."
        )

    tracking_clause = ""
    if action_name == "track_order":
        tracking_clause = (
            " This is an order tracking reply — explicitly and clearly state three "
            "things when they're present in the data: the shipment's current status "
            "(e.g. in transit, out for delivery, delivered), its current location "
            "(current_location), and the estimated delivery date (estimated_delivery). "
            "Don't bury these in vague phrasing — name them plainly, each in its own "
            "clause. If current_location is missing or null, say the status clearly "
            "anyway and mention that a precise location isn't available yet, offering "
            "the tracking link (tracking_url) so the shopper can check directly with "
            "the carrier. Never state a location, status, or delivery date that isn't "
            "literally present in the data."
        )

    extra_clause = ""
    if action_name in ("list_recent_orders", "get_my_orders"):
        extra_clause = (
            " When listing orders, if active_only is true or the user asked for active orders, introduce them specifically as their active orders in progress and summarize only those active orders. Summarize them clearly with order number (e.g. #1001), "
            "date, status, items with quantities, and total price. If no orders exist, mention that politely."
        )
    elif action_name == "check_return_eligibility":
        extra_clause = (
            " When checking return eligibility: "
            "Speak conversationally in simple, friendly words like an in-store assistant. "
            "If status is 'order_number_required', kindly ask for the order number and explain that our store has a 30-day return policy for unused items in original packaging. "
            "If status is 'unfulfilled', explain clearly that this order hasn't been delivered yet, so returns are not possible yet, but offer to cancel the order or update the shipping address before it ships. "
            "If status is 'in_transit', explain that the order is on its way, and they can easily initiate a return as soon as it arrives at their door. "
            "If status is 'eligible', warmly confirm that the order is within the 30-day return window, explain the condition requirements (unused, in original packaging with tags), and ask if they would like you to start the return process for them. "
            "If status is 'expired', kindly explain that the 30-day return window has passed, but let them know that if the item arrived defective or damaged, they can file a warranty claim instead. "
            "If status is 'order_not_found', politely ask them to check the order number."
        )
    elif action_name in ("request_order_return", "return_order"):
        extra_clause = (
            " When submitting an order return request: "
            "Reassure the shopper warmly and confirm their return request has been submitted for order number (order_number). "
            "Mention that our team will send prepaid return instructions to their email, and ask if there is anything else they need help with."
        )
    elif action_name == "submit_claim":
        extra_clause = (
            " State the warranty claim ID (claim_id), the order number, the recorded issue, "
            "and reassure the customer that the claim is under review with a 24-48 business hours response window."
        )
    elif action_name == "check_claim_status":
        extra_clause = (
            " Clearly state the current warranty claim status and review details from the data."
        )
    elif action_name == "answer_policy_question":
        extra_clause = (
            " Answer the customer's policy question directly, concisely, and accurately based on the policy body provided."
        )
    elif action_name == "initiate_payment":
        extra_clause = (
            " Confirm that the shopper is being taken to the secure checkout with their cart items. "
            "Never ask for card numbers or payment credentials in chat."
        )
    elif action_name == "confirm_payment_otp":
        extra_clause = (
            " Enthusiastically confirm that the payment was successful, mentioning the order number, total amount paid, "
            "payment method, and estimated 3-5 business days delivery timeframe."
        )
    elif action_name == "search_products":
        extra_clause = (
            " Present the matching products found with their names and prices in a concise, friendly manner. "
            "If an item is marked out of stock (in_stock is false or available is false), clearly let the shopper know that it is currently out of stock and offer to notify them when fresh stock arrives. "
            "Keep the response concise and focused strictly on the searched item(s). "
            "If 'no_exact_match' is true or no products are found in the results, politely let the shopper know that no products matching that name were found in our store catalog."
        )
    elif action_name == "assistant_help":
        extra_clause = (
            " Warmly welcome the shopper to the store, introduce what you can do (search products, manage cart, "
            "track orders, file warranty claims, check store policies, and fast OTP checkout), and invite them to explore."
        )
    elif action_name in ("recommend_products", "get_recommendations"):
        extra_clause = (
            " Enthusiastically recommend the products provided in the data, highlighting their names and prices, "
            "and offer to add any item to their cart."
        )
    elif action_name == "add_item":
        extra_clause = (
            " Confirm clearly that the requested product and exact quantity (quantity) have been added to the cart."
        )
    elif action_name == "remove_item":
        extra_clause = (
            " When removing an item, if a specific quantity was removed (quantity_removed) and units still remain in the cart (remaining_quantity > 0), "
            "explicitly confirm that you removed that specific quantity and state how many units remain. "
            "Only claim the item was completely removed or that the cart is empty if remaining_quantity is 0."
        )
    elif action_name == "clarify":
        extra_clause = (
            " Keep the shopper engaged and interested! Warmly ask how you can assist them today with browsing items, "
            "tracking an order, managing their cart, or checking store policies."
        )
    elif action_name == "subscribe_restock":
        extra_clause = (
            " For restock / stock alerts: "
            "If status is 'already_in_stock', enthusiastically tell the shopper that the product is currently IN STOCK and available to purchase right now, so they don't have to wait for a restock! Invite them to check out the item card. "
            "If status is 'not_found', kindly let them know the product could not be found in the store catalog and suggest double-checking the product name. "
            "If status is 'email_required', inform them the product is out of stock and warmly ask for their email address so we can alert them the second it arrives. "
            "If status is 'alert_subscribed', confirm cheerfully that they are subscribed and will receive an email as soon as fresh stock arrives."
        )
    elif action_name == "check_availability":
        extra_clause = (
            " When the customer asks about product availability or stock: "
            "Chat like a friendly in-store retail sales assistant in simple, everyday human words. "
            "If status is 'product_required', warmly ask which specific product or style they'd like you to check. "
            "If status is 'available', cheerfully confirm it is in stock right now, state its price, and warmly ask if they'd like help adding it to their cart or choosing a size. "
            "If status is 'out_of_stock', kindly explain that it's currently sold out, mention any close in-stock alternatives provided, and ask if they'd like to check out an alternative or get notified when fresh stock arrives. "
            "If status is 'not_found', warmly let them know the item wasn't found in the catalog and suggest checking the name or looking at popular store items."
        )

    system_prompt = (
        f"You are a friendly, helpful store sales assistant. Reply ONLY in {language_name} "
        f"({language}), regardless of what language this instruction is written in. "
        "Keep the reply warm, natural, and easy to understand for an everyday shopper. "
        "Speak in simple conversational words to keep the dialogue flowing smoothly. "
        "NEVER use heavy technical or system jargon (never say 'endpoint', 'variant ID', 'parameters', 'payload', 'schema', 'null', 'JSON', 'dispatched'). "
        "Use the structured data given to you as the source of truth — do not invent "
        f"details that aren't in it.{rag_clause}{tracking_clause}{extra_clause} If the data indicates "
        "an error or empty result, say so gently and suggest what the user could try next."
    )

    user_prompt_parts = [
        f"User's original message: {original_message}",
        f"Action performed: {action_name}",
        f"Result data:\n{json.dumps(data, ensure_ascii=False, indent=2)}",
    ]
    if rag_context_text:
        user_prompt_parts.append(f"Retrieved reference material:\n{rag_context_text}")
    user_prompt_parts.append(f"Write the reply in {language_name}.")
    user_prompt = "\n\n".join(user_prompt_parts)

    try:
        response = llm_selector.create_chat_completion(
            temperature=0.4,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        return _strip_thinking(response.choices[0].message.content)
    except Exception as e:
        print(f"reply_generator: LLM generation failed ({e!r}) — using resilient template")
        if isinstance(data, dict):
            if data.get("message"):
                return str(data["message"])
            if data.get("results") and isinstance(data["results"], list):
                count = len(data["results"])
                return f"I found {count} product{'s' if count != 1 else ''} that might match what you're looking for:"
            if data.get("orders") and isinstance(data["orders"], list):
                count = len(data["orders"])
                if data.get("active_only"):
                    return f"Here {'are' if count != 1 else 'is'} your {count} active order{'s' if count != 1 else ''} in progress:"
                return f"Here {'are' if count != 1 else 'is'} your {count} recent order{'s' if count != 1 else ''}:"
            if data.get("order_number") or data.get("order_id"):
                oid = data.get("order_number") or data.get("order_id")
                st = data.get("status") or "processing"
                return f"Order #{oid} is currently {st}."
        return "I'm here to help, but I'm having a brief issue generating a detailed reply. Please check back shortly or let me know what you need."


if __name__ == "__main__":
    # Quick manual test — run: python reply_generator.py
    sample_data = {
        "order_id": "1042",
        "status": "shipped",
        "carrier": "FedEx",
        "estimated_delivery": "Thursday",
    }
    for lang in ["en", "hi", "es"]:
        reply = generate_reply("track_order", sample_data, lang, "Where is my order?")
        print(f"\n[{lang}] {reply}")

    # Example of a RAG-backed policy answer:
    # from rag_retriever import retrieve_context
    # chunks = retrieve_context("what is your return policy")
    # reply = generate_reply(
    #     "answer_policy_question", {}, "en",
    #     "What is your return policy?", rag_context=chunks
    # )
    # print(reply)
