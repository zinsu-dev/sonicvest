"""
GLO Payment Gateway Configuration
Docs: https://glopayment.net/#/document

Reads credentials from environment variables first, then falls back to
sensible defaults. Values that MUST be provided from the GLO merchant
backend (System Management -> Account Management) are left blank so the
service can flag them clearly instead of silently using wrong data.
"""

import os

from dotenv import load_dotenv


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)


def _bool_env(name: str, default: bool) -> bool:
    value = (os.environ.get(name) or '').strip().lower()
    if not value:
        return default
    return value in ('1', 'true', 'yes', 'on')


def _float_env(name: str, default: float) -> float:
    value = (os.environ.get(name) or '').strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


# Single merchant key is used to sign both collection (pay) and payout requests.
_SECRET_KEY = os.environ.get('GLO_SECRET_KEY', '').strip()

GTR_CONFIG = {
    # --- Account / host ---
    'MERCHANT_ID': os.environ.get('GLO_MERCHANT_ID', '500001048').strip(),
    'SECRET_KEY': _SECRET_KEY,
    'API_HOST': os.environ.get('GLO_API_HOST', 'https://glopayment.net').strip().rstrip('/'),
    'ENABLED': _bool_env('GLO_ENABLED', True),
    'REQUEST_TIMEOUT': _float_env('GLO_REQUEST_TIMEOUT', 15.0),

    # --- Collection (deposit) ---
    'CHANNEL_CODE': os.environ.get('GLO_CHANNEL_CODE', '').strip(),
    'MIN_AMOUNT': _float_env('GLO_MIN_AMOUNT', 100.0),
    'MAX_AMOUNT': _float_env('GLO_MAX_AMOUNT', 1000000.0),

    # Fallback customer details when a value is missing on the account.
    'DEFAULT_CUSTOMER_NAME': os.environ.get('GLO_DEFAULT_CUSTOMER_NAME', 'SONICVEST User').strip(),
    'DEFAULT_CUSTOMER_EMAIL': os.environ.get('GLO_DEFAULT_CUSTOMER_EMAIL', 'support@sonicvest.com').strip(),
    'DEFAULT_CUSTOMER_MOBILE': os.environ.get('GLO_DEFAULT_CUSTOMER_MOBILE', '08000000000').strip(),

    # --- Payout (代付) ---
    'PAYOUT_SECRET_KEY': os.environ.get('GLO_PAYOUT_SECRET_KEY', '').strip() or _SECRET_KEY,
    'PAYOUT_CHANNEL_CODE': os.environ.get('GLO_PAYOUT_CHANNEL_CODE', '').strip(),
    'PAYOUT_BANK_CODE': os.environ.get('GLO_PAYOUT_BANK_CODE', '').strip(),
    'TRANSFER_MIN_AMOUNT': _float_env('GLO_PAYOUT_MIN_AMOUNT', 100.0),
    'TRANSFER_MAX_AMOUNT': _float_env('GLO_PAYOUT_MAX_AMOUNT', 1000000.0),
}
