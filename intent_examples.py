"""
intent_examples.py
------------------
Example phrases per (intent, action), used by semantic.route_intent() to
find the closest meaning when the LLM classifier gives up.

More and more varied examples = better matching. Add real messages from
your logs here (especially ones that ended up as "fallback"). Keep a mix of
English, Hinglish (Hindi in English letters), Hindi and Punjabi, matching
what your shoppers actually write.
"""

EXAMPLES: dict[tuple[str, str], list[str]] = {
    ("customer_account", "get_my_orders"): [
        "show all my orders", "what have I ordered so far", "show my order history",
        "list my past and current orders", "which orders have I placed", "do I have any pending orders",
        "what did I buy from you before", "show my previous purchases", "show orders for my account",
        "mere saare orders dikhao", "meri purani orders ki list dikhao", "maine ab tak kya kya order kiya hai",
        "मेरे सारे ऑर्डर दिखाओ", "मेरे पिछले ऑर्डर की जानकारी दो",
        "ਮੇਰੇ ਸਾਰੇ ਆਰਡਰ ਦਿਖਾਓ", "ਮੈਂ ਹੁਣ ਤੱਕ ਕੀ ਆਰਡਰ ਕੀਤਾ ਹੈ", "ਮੇਰੇ ਪੁਰਾਣੇ ਆਰਡਰ ਦੀ ਲਿਸਟ ਦਿਖਾਓ",
    ],
    ("order_tracking", "list_recent_orders"): [
        "show all my orders", "show my orders", "list all my orders", "show all orders for test@example.com",
        "list active orders", "show my active orders", "what are my active orders", "orders in progress", "active orders only",
        "what are my past orders", "my order history", "show my purchases",
        "mere saare orders dikhao", "mere orders dikhao", "order history dikhao",
        "मेरे सभी ऑर्डर दिखाओ", "ਮੇਰੇ ਸਾਰੇ ਆਰਡਰ ਦਿਖਾਓ",
    ],
    ("customer_account", "get_recommendations"): [
        "recommend something for me", "what would you suggest for me", "suggest products I might like",
        "what should I buy next", "any picks based on my past orders", "show me things similar to what I bought",
        "I don't know what to get, help me choose", "surprise me with something I'd like",
        "mere liye kuch recommend karo", "mujhe kya lena chahiye", "mere pasand ka kuch dikhao",
        "मेरे लिए कुछ सुझाइए", "मुझे क्या खरीदना चाहिए",
        "ਮੇਰੇ ਲਈ ਕੁਝ ਸੁਝਾਓ", "ਮੈਨੂੰ ਕੀ ਖਰੀਦਣਾ ਚਾਹੀਦਾ ਹੈ",
    ],
    ("product_search", "search_products"): [
        "show me running shoes", "do you have anything in blue", "I'm looking for something warm for winter",
        "what's your cheapest t-shirt", "find me a gift under 1000 rupees", "browse your collection",
        "anything good for summer", "I need something to wear to a wedding",
        "kya aapke paas hoodie hai", "mujhe ek achi t-shirt dikhao", "sasta jacket dikhao",
        "मुझे जूते दिखाओ", "कोई अच्छी टी-शर्ट है क्या",
        "ਮੈਨੂੰ ਟੀ-ਸ਼ਰਟ ਦਿਖਾਓ", "ਕੀ ਤੁਹਾਡੇ ਕੋਲ ਹੂਡੀ ਹੈ",
    ],
    ("order_tracking", "track_order"): [
        "where is my order", "track order 1042", "has my package shipped yet", "when will my order arrive",
        "delivery status of my parcel", "my order hasn't arrived", "track my package", "where is order #1001",
        "mera order kahan hai", "mere order ka status batao", "parcel kab tak aayega",
        "मेरा ऑर्डर कहाँ है", "ਮੇਰਾ ਆਰਡਰ ਕਿੱਥੇ ਹੈ", "ਮੇਰਾ ਪਾਰਸਲ ਕਦੋਂ ਆਵੇਗਾ",
    ],
    ("cart_management", "view_cart"): [
        "what's in my cart", "show my cart", "what have I added to my basket", "open my bag",
        "view cart items", "how much is in my cart", "mera cart dikhao", "cart mein kya hai",
        "मेरा कार्ट दिखाओ", "ਮੇਰਾ ਕਾਰਟ ਦਿਖਾਓ",
    ],
    ("cart_management", "add_item"): [
        "add this to my cart", "put the blue hoodie in my bag", "I want to buy this one",
        "add 2 shirts to cart", "add black shoes to cart", "put mug in my cart",
        "isse cart mein daalo", "yeh wali shirt cart mein add karo", "इसे कार्ट में डालो", "ਇਹ ਕਾਰਟ ਵਿੱਚ ਪਾਓ",
    ],
    ("cart_management", "remove_item"): [
        "remove the shirt from my cart", "take that out of my basket", "I don't want the hoodie anymore",
        "delete mug from cart", "remove item from cart", "cart se hatao", "isse cart se hatao", "इसे कार्ट से हटाओ",
    ],
    ("cart_management", "edit_quantity"): [
        "change the quantity to 2", "make it three of those", "I need two instead of one",
        "update quantity of hoodie to 4", "change quantity of shirt to 1", "quantity badal ke 2 kar do",
    ],
    ("cart_management", "clear_cart"): [
        "empty my cart", "remove everything from my cart", "clear my basket", "cart khali karo", "कार्ट खाली करो",
    ],
    ("warranty_claim", "submit_claim"): [
        "this arrived broken and I want a replacement", "the item is damaged, how do I claim warranty",
        "I want to claim warranty for order #1001", "earphones stopped working, file warranty claim",
        "my zipper is broken, need warranty replacement", "I received a defective product, need warranty claim",
        "product kharab aaya hai", "warranty claim karni hai",
        "प्रोडक्ट खराब आया है", "ਸਮਾਨ ਖਰਾਬ ਆਇਆ ਹੈ",
    ],
    ("order_management", "check_return_eligibility"): [
        "whether i can return my order number 1003 or not", "can I return order #1003",
        "can I return my order 1003", "is order 1003 eligible for return", "can I return my order",
        "check return policy for order 1003", "is order #1003 returnable", "kya main order 1003 return kar sakta hoon",
        "order 1003 return ho sakta hai kya", "check return eligibility for 1003",
    ],
    ("order_management", "request_order_return"): [
        "return order #1003", "I want to return order 1003", "please start return for order #1003",
        "return my order 1003", "initiate return for order 1003", "mujhe order 1003 return karna hai",
        "order 1003 return karo",
    ],
    ("order_management", "cancel_order"): [
        "cancel order #1003", "I want to cancel my order 1003", "please cancel order 1001", "cancel my order",
        "order cancel kardo",
    ],
    ("order_management", "edit_address"): [
        "change address for order #1003", "update shipping address for order 1003", "edit delivery address for order 1001",
        "delivery address change karo",
    ],
    ("warranty_claim", "check_claim_status"): [
        "what's the status of my warranty claim", "has my return been approved", "any update on my replacement request",
        "check warranty status for order #1001", "status of claim CLM-1001", "claim status check karo",
    ],
    ("payment_checkout", "save_payment_credentials"): [
        "save my payment credentials", "store my credit card details", "update my payment card",
        "save card for 1-click checkout", "save my visa card", "store payment method",
        "payment details save karo", "card save karo", "card credentials store kardo",
    ],
    ("payment_checkout", "initiate_payment"): [
        "pay for my cart", "checkout and pay", "I want to pay now", "proceed to payment with saved card",
        "buy now and pay", "pay for my order", "complete my purchase", "pay with my card",
        "cart ka payment kardo", "order pay karo", "mujhe payment karni hai", "pay now",
    ],
    ("payment_checkout", "confirm_payment_otp"): [
        "my OTP is 123456", "here is the otp 492015", "confirm payment with OTP 859302",
        "authorize payment 736201", "OTP is 582910", "492015", "123456", "otp verify karo",
        "mera otp hai 123456",
    ],
    ("policy_query", "answer_policy_question"): [
        "what's your refund policy", "how long does shipping take", "do you ship internationally",
        "can I return an item after 30 days", "do you share my data with anyone", "what are the delivery charges",
        "what is your warranty policy", "return policy kya hai", "shipping mein kitna time lagta hai",
        "delivery charges kitne hain", "warranty terms kya hain", "can I pay with UPI", "can i pay with upi",
        "what payment methods do you accept", "do you take credit cards", "can I pay with cash on delivery",
        "payment options kya hain", "आपकी रिफंड पॉलिसी क्या है", "ਤੁਹਾਡੀ ਰਿਫੰਡ ਪਾਲਿਸੀ ਕੀ ਹੈ",
    ],
    ("registration_login", "register"): [
        "I want to create an account", "sign me up", "how do I register", "account banana hai",
    ],
    ("registration_login", "login"): [
        "log me in", "I want to sign in", "how do I log into my account", "login kaise karu",
    ],
    ("registration_login", "logout"): [
        "sign me out", "log out of my account", "logout karna hai",
    ],
    ("registration_login", "forgot_password"): [
        "I forgot my password", "reset my password", "can't log in, need a new password", "password bhool gaya",
    ],
    ("fallback", "clarify"): [
        "hmm", "can you help me with something", "asdkfj", "hello", "hi there", "ok", "thanks", "what can you do",
    ],
}
