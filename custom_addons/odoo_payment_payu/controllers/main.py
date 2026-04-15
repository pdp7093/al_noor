import pprint

from odoo.addons.odoo_payment_payu import const as payu_consts
from odoo.addons.odoo_payment_payu import utils as payu_utils
from odoo.addons.payment.logging import get_payment_logger
from odoo.http import Controller, request, route
from werkzeug.exceptions import Forbidden

_logger = get_payment_logger(__name__)


class PayUController(Controller):

    RETURN_URL = '/payment/payu/return'
    WEBHOOK_URL = '/payment/payu/webhook'

    # --------------------------
    # RETURN (FAST PROCESSING)
    # --------------------------
    @route(RETURN_URL, type='http', auth='public', methods=['GET', 'POST'])
    def payu_return_from_checkout(self, **payload):
        _logger.info('🔥 PayU RETURN payload:\n%s', pprint.pformat(payload))

        try:
            tx_sudo = request.env['payment.transaction'].sudo()._search_by_reference('payu', payload)

            if tx_sudo:
                _logger.info("⚡ Processing transaction from RETURN (fast flow)")

                # Process instantly (NO WAIT)
                tx_sudo._process('payu', payload)

            else:
                _logger.warning("❌ Transaction not found in RETURN")

        except Exception as e:
            _logger.exception("❌ Error in RETURN processing: %s", e)

        return request.redirect('/payment/status')

    # --------------------------
    # WEBHOOK (BACKGROUND SAFETY)
    # --------------------------
    @route(WEBHOOK_URL, type='http', auth='public', methods=['POST'], csrf=False)
    def payu_webhook(self, **payload):

        if request.httprequest.content_type == 'application/json':
            payload = request.httprequest.get_json(silent=True)

        # DEBUG only (no spam in production)
        _logger.debug('PayU WEBHOOK payload:\n%s', pprint.pformat(payload))

        tx_sudo = request.env['payment.transaction'].sudo()._search_by_reference('payu', payload)

        if not tx_sudo:
            _logger.warning('No matching transaction for webhook')
            return request.make_json_response('')

        try:
            # 🔐 Signature verification
            PayUController._verify_signature(payload, tx_sudo)

            # Process
            tx_sudo._process('payu', payload)

            _logger.debug('Webhook processed (Ref: %s)', tx_sudo.reference)

        except Exception as e:
            _logger.error('Webhook processing failed: %s', str(e))

        return request.make_json_response('')

    # --------------------------
    # SIGNATURE VERIFY
    # --------------------------
    @staticmethod
    def _verify_signature(payment_data, tx_sudo):

        if 'action' in payment_data and payment_data.get('action').strip() == 'refund':
            if tx_sudo.provider_id.payu_merchant_key == payment_data.get('key').strip():
                return True
            raise Forbidden()

        received_hash = (payment_data.get('hash') or '').strip()

        payment_data['salt'] = tx_sudo.provider_id.payu_merchant_salt
        payment_data['key'] = tx_sudo.provider_id.payu_merchant_key

        computed_hash = payu_utils.generate_payu_hash(
            payment_data,
            payu_consts.PAYU_HASH_SEQUENCE.get('PAYMENT_WEBHOOK')
        )

        if computed_hash == received_hash:
            return True

        raise Forbidden()