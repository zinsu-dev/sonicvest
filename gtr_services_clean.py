"""
GLO Payment Gateway Integration
Docs: https://glopayment.net/#/document

Signature algorithm (identical for pay / payout / callbacks):
    1. Drop empty values and the `sign` field.
    2. Sort remaining params by key (ascending) and join as `k=v&k=v`.
    3. Append `&gloKeys=<key>`.
    4. digest = HMAC-SHA512(step3, key)  -> raw bytes
    5. sign   = md5( base64(digest) )    -> lowercase hex

Notes from the documentation:
    * Content-Type is application/x-www-form-urlencoded (form POST).
    * amount must always carry two decimal places (e.g. "100.00").
    * The notify (callback) URL is configured in the GLO merchant backend
      (System Management -> Account Management), not sent per request.
    * Deposit sync response returns {status, msg, url}; redirect user to `url`.
    * Callbacks arrive as JSON with returnCode "00" == success; reply lowercase "ok".
"""

import base64
import hashlib
import hmac
from datetime import datetime

import requests
from requests.exceptions import ConnectTimeout, ReadTimeout, RequestException


class GTRPayService:
    """Client for the GLO payment gateway (collection + payout)."""

    def __init__(self, mch_id=None, secret_key=None):
        try:
            from gtr_config import GTR_CONFIG

            self.mch_id = mch_id or GTR_CONFIG.get('MERCHANT_ID', '')
            self.secret_key = secret_key or GTR_CONFIG.get('SECRET_KEY', '')
            self.api_host = (GTR_CONFIG.get('API_HOST') or 'https://glopayment.net').rstrip('/')
            self.enabled = GTR_CONFIG.get('ENABLED', True)
            self.request_timeout = float(GTR_CONFIG.get('REQUEST_TIMEOUT', 15.0) or 15.0)

            self.channel_code = GTR_CONFIG.get('CHANNEL_CODE', '')
            self.min_amount = float(GTR_CONFIG.get('MIN_AMOUNT', 100.0) or 0.0)
            self.max_amount = float(GTR_CONFIG.get('MAX_AMOUNT', 1000000.0) or 0.0)

            self.default_customer_name = GTR_CONFIG.get('DEFAULT_CUSTOMER_NAME', 'SONICVEST User')
            self.default_customer_email = GTR_CONFIG.get('DEFAULT_CUSTOMER_EMAIL', 'support@sonicvest.com')
            self.default_customer_mobile = GTR_CONFIG.get('DEFAULT_CUSTOMER_MOBILE', '08000000000')

            self.payout_secret_key = GTR_CONFIG.get('PAYOUT_SECRET_KEY', '') or self.secret_key
            self.payout_channel_code = GTR_CONFIG.get('PAYOUT_CHANNEL_CODE', '')
            # Kept as `bank_code` for backwards compatibility with admin routes.
            self.bank_code = GTR_CONFIG.get('PAYOUT_BANK_CODE', '')
            self.transfer_min_amount = float(GTR_CONFIG.get('TRANSFER_MIN_AMOUNT', 100.0) or 0.0)
            self.transfer_max_amount = float(GTR_CONFIG.get('TRANSFER_MAX_AMOUNT', 1000000.0) or 0.0)
        except ImportError:
            print('⚠️ GLO config not found - using safe defaults (gateway disabled)')
            self.mch_id = mch_id or ''
            self.secret_key = secret_key or ''
            self.api_host = 'https://glopayment.net'
            self.enabled = False
            self.request_timeout = 15.0
            self.channel_code = ''
            self.min_amount = 100.0
            self.max_amount = 1000000.0
            self.default_customer_name = 'SONICVEST User'
            self.default_customer_email = 'support@sonicvest.com'
            self.default_customer_mobile = '08000000000'
            self.payout_secret_key = ''
            self.payout_channel_code = ''
            self.bank_code = ''
            self.transfer_min_amount = 100.0
            self.transfer_max_amount = 1000000.0

    # ------------------------------------------------------------------ #
    # Signing helpers
    # ------------------------------------------------------------------ #
    def build_sign_digest(self, data, secret_key):
        """Return the GLO MD5(base64(HMAC-SHA512)) signature for `data`."""
        filtered = []
        for key, value in data.items():
            if key in ('sign', 'signType'):
                continue
            if value is None:
                continue
            value = str(value)
            if value == '':
                continue
            filtered.append((key, value))

        sign_string = '&'.join(f'{key}={value}' for key, value in sorted(filtered))
        sign_string = f'{sign_string}&gloKeys={secret_key}'

        digest = hmac.new(secret_key.encode('utf-8'), sign_string.encode('utf-8'), hashlib.sha512).digest()
        return hashlib.md5(base64.b64encode(digest)).hexdigest()

    def _verify_signature(self, callback_data, secret_key):
        """Validate a callback signature. amount is normalised to 2 decimals."""
        received_sign = str(callback_data.get('sign') or '')
        if not received_sign:
            return False

        payload = {k: v for k, v in callback_data.items() if k not in ('sign', 'signType')}

        # GLO signs the amount with two decimal places even though JSON may
        # deliver it as a number (100.00 -> 100.0 after JSON parsing).
        candidates = [dict(payload)]
        if 'amount' in payload and payload['amount'] not in (None, ''):
            try:
                normalised = dict(payload)
                normalised['amount'] = f'{float(payload["amount"]):.2f}'
                candidates.insert(0, normalised)
            except (TypeError, ValueError):
                pass

        for candidate in candidates:
            expected = self.build_sign_digest(candidate, secret_key)
            if hmac.compare_digest(expected.lower(), received_sign.lower()):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Collection (deposit)
    # ------------------------------------------------------------------ #
    def create_deposit_payment(
        self,
        amount=None,
        reference=None,
        name=None,
        email=None,
        mobile=None,
        channel_code=None,
        callback_url=None,   # configured in GLO backend; accepted for compatibility
        page_url=None,       # configured in GLO backend; accepted for compatibility
        return_url=None,
        mch_return_msg=None,
        goods_name='Account Deposit',
        **kwargs,
    ):
        """Create a GLO collection order and return the checkout URL."""
        try:
            if not self.enabled:
                return {'success': False, 'message': 'GLO payment is not enabled'}
            if not self.secret_key:
                return {'success': False, 'message': 'GLO secret key is not configured'}
            if not self.channel_code and not channel_code:
                return {'success': False, 'message': 'GLO channel code is not configured'}

            amount_value = float(amount)
            if self.min_amount and amount_value < self.min_amount:
                return {'success': False, 'message': f'Minimum automatic deposit is ₦{self.min_amount:,.0f}'}
            if self.max_amount and amount_value > self.max_amount:
                return {'success': False, 'message': f'Maximum automatic deposit is ₦{self.max_amount:,.0f}'}

            request_data = {
                'merchantId': str(self.mch_id),
                'orderId': str(reference),
                'channelCode': str(channel_code or self.channel_code),
                'amount': f'{amount_value:.2f}',
                'name': str(name or self.default_customer_name),
                'email': str(email or self.default_customer_email),
                'mobile': str(mobile or self.default_customer_mobile),
            }
            request_data['sign'] = self.build_sign_digest(request_data, self.secret_key)

            endpoint = f'{self.api_host}/pay/order/actions/commit'
            print(f'🔄 GLO Pay Request: {endpoint}')
            print(f'📊 Request Data: {request_data}')

            response = requests.post(
                endpoint,
                data=request_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=self.request_timeout,
            )

            print(f'📥 GLO Pay Response Status: {response.status_code}')
            print(f'📄 GLO Pay Response: {response.text}')

            if response.status_code != 200:
                return {'success': False, 'message': f'API request failed with status {response.status_code}'}

            try:
                result = response.json()
            except Exception:
                return {'success': False, 'message': f'Invalid JSON response: {response.text}'}

            if str(result.get('status')) != '200':
                return {
                    'success': False,
                    'message': result.get('msg') or 'Failed to create payment',
                    'raw_response': result,
                }

            payment_url = result.get('url')
            if not payment_url:
                return {
                    'success': False,
                    'message': 'Payment URL missing from GLO response',
                    'raw_response': result,
                }

            return {
                'success': True,
                'payment_url': payment_url,
                'trade_no': reference,  # GLO issues its platform order id in the callback
                'reference': reference,
                'message': result.get('msg') or 'Payment created successfully',
                'raw_response': result,
            }
        except (ConnectTimeout, ReadTimeout):
            return {
                'success': False,
                'message': f'Payment gateway timed out after {self.request_timeout:.0f} seconds. Please try again shortly.',
            }
        except RequestException as e:
            print(f'❌ GLO Pay Error: {str(e)}')
            return {'success': False, 'message': f'Payment gateway request failed: {str(e)}'}
        except ValueError:
            return {'success': False, 'message': 'Invalid amount format'}
        except Exception as e:
            print(f'❌ GLO Pay Error: {str(e)}')
            return {'success': False, 'message': f'Error creating payment: {str(e)}'}

    def verify_payment_callback(self, callback_data):
        """Verify a GLO collection (deposit) callback."""
        try:
            if not callback_data.get('sign'):
                return {'success': False, 'message': 'No signature in callback'}

            if not self._verify_signature(callback_data, self.secret_key):
                return {'success': False, 'message': 'Invalid signature'}

            if str(callback_data.get('returnCode')) != '00':
                return {
                    'success': False,
                    'message': f"Payment not successful: returnCode={callback_data.get('returnCode')}",
                    'status': 'failed',
                }

            return {
                'success': True,
                'reference': callback_data.get('merchantOrderId'),
                'trade_no': callback_data.get('orderId'),
                'amount': callback_data.get('amount'),
                'status': 'completed',
                'merchant_id': callback_data.get('merchantId'),
            }
        except Exception as e:
            return {'success': False, 'message': f'Error verifying callback: {str(e)}'}

    def verify_deposit_payment(self, reference=None, trade_no=None, amount=None):
        """GLO has no public order-query API; confirmation is via callback only."""
        return {
            'success': True,
            'verified': False,
            'reference': reference,
            'trade_no': trade_no,
            'status': 'pending',
            'message': 'Payment is confirmed automatically once GLO sends the callback. Please wait a moment.',
        }

    # ------------------------------------------------------------------ #
    # Payout (代付)
    # ------------------------------------------------------------------ #
    def create_transfer_payment(
        self,
        amount=None,
        transfer_id=None,
        bank_code=None,
        receive_name=None,
        receive_account=None,
        number=None,
        email=None,
        mobile=None,
        channel_code=None,
        remark=None,
        back_url=None,
        apply_date=None,
    ):
        """Create a GLO payout (代付) order."""
        try:
            if not self.enabled:
                return {'success': False, 'message': 'GLO payment is not enabled'}
            if not self.payout_secret_key:
                return {'success': False, 'message': 'GLO payout key is not configured'}
            if not self.payout_channel_code and not channel_code:
                return {'success': False, 'message': 'GLO payout channel code is not configured'}

            amount_value = float(amount)
            if self.transfer_min_amount and amount_value < self.transfer_min_amount:
                return {'success': False, 'message': f'Minimum transfer amount is ₦{self.transfer_min_amount:,.0f}'}
            if self.transfer_max_amount and amount_value > self.transfer_max_amount:
                return {'success': False, 'message': f'Maximum transfer amount is ₦{self.transfer_max_amount:,.0f}'}

            transfer_id_value = transfer_id or f'GW{datetime.now().strftime("%m%d%H%M%S")}'

            request_data = {
                'merchantId': str(self.mch_id),
                'orderId': str(transfer_id_value),
                'channelCode': str(channel_code or self.payout_channel_code),
                'amount': f'{amount_value:.2f}',
                'name': str(receive_name or ''),
                'account': str(receive_account or ''),
                'bankCode': str(bank_code or self.bank_code),
                'number': str(number or receive_account or ''),
                'email': str(email or self.default_customer_email),
                'mobile': str(mobile or self.default_customer_mobile),
            }
            if remark:
                request_data['backup'] = str(remark)

            request_data['sign'] = self.build_sign_digest(request_data, self.payout_secret_key)

            endpoint = f'{self.api_host}/payment/order/actions/commit'
            print(f'🔄 GLO Payout Request: {endpoint}')
            print(f'📊 Payout Request Data: {request_data}')

            response = requests.post(
                endpoint,
                data=request_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=self.request_timeout,
            )

            print(f'📥 GLO Payout Response Status: {response.status_code}')
            print(f'📄 GLO Payout Response: {response.text}')

            if response.status_code != 200:
                return {'success': False, 'message': f'API request failed with status {response.status_code}'}

            try:
                result = response.json()
            except Exception:
                return {'success': False, 'message': f'Invalid JSON response: {response.text}'}

            if str(result.get('status')) != '200':
                return {
                    'success': False,
                    'message': result.get('msg') or 'Failed to create transfer',
                    'raw_response': result,
                }

            return {
                'success': True,
                'message': result.get('msg') or 'Transfer request accepted',
                'transfer_id': transfer_id_value,
                'trade_no': transfer_id_value,
                'amount': f'{amount_value:.2f}',
                'apply_date': apply_date,
                'raw_response': result,
            }
        except (ConnectTimeout, ReadTimeout):
            return {
                'success': False,
                'message': f'Transfer gateway timed out after {self.request_timeout:.0f} seconds. Please try again shortly.',
            }
        except RequestException as e:
            print(f'❌ GLO Payout Error: {str(e)}')
            return {'success': False, 'message': f'Transfer gateway request failed: {str(e)}'}
        except ValueError:
            return {'success': False, 'message': 'Invalid amount format'}
        except Exception as e:
            print(f'❌ GLO Payout Error: {str(e)}')
            return {'success': False, 'message': f'Error creating transfer: {str(e)}'}

    def verify_transfer_callback(self, callback_data):
        """Verify a GLO payout (代付) callback."""
        try:
            if not callback_data.get('sign'):
                return {'success': False, 'message': 'No signature in callback'}

            if not self._verify_signature(callback_data, self.payout_secret_key):
                return {'success': False, 'message': 'Invalid signature'}

            success = str(callback_data.get('returnCode')) == '00'
            return {
                'success': success,
                'reference': callback_data.get('merchantOrderId'),
                'trade_no': callback_data.get('orderId'),
                'amount': callback_data.get('amount'),
                'status': 'completed' if success else 'failed',
                'merchant_id': callback_data.get('merchantId'),
                'message': 'Transfer successful' if success else f"Transfer failed: returnCode={callback_data.get('returnCode')}",
            }
        except Exception as e:
            return {'success': False, 'message': f'Error verifying transfer callback: {str(e)}'}


# Create global instance for import
gtr_pay_service = GTRPayService()
