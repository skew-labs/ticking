# Security boundaries

Do not file credentials, customer data or exploitable payment details in public issues. Use the repository's private security reporting feature when available; otherwise request a private contact without including the sensitive material.

- Collectors and the terminal do not hold customer signing keys or place orders.
- API keys are server-side secrets. Browser embeds receive origin-bound, short-lived display grants.
- Payment verification checks the expected network, token, payer, recipient, amount, reference, finality and balance effects. A submitted signature alone is not credit.
- Integer-unit postings are balanced and sealed. Reservations, bounded budgets and idempotency keys protect retries and abandoned work.
- Unavailable, malformed, stale or mismatched market data must not be promoted to a successful paid read.
- Original input validation and recovery are separate from public serving. Protect the private bridge and data directories; never expose them as static files.
- Bounded queues and storage reserves are required. Capacity limits are not removed to conceal overload.

Run tests against isolated databases. Changing keys, funding accounts, signing transactions, deleting originals, purchasing source access or deploying publicly requires the operator's separate authority.
