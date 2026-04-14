# payment_payu/controllers/main.py
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

PAYMENT_TRANSACTION_MODEL = 'payment.transaction'


class PayUController(http.Controller):

    _webhook_url = '/payment/payu/webhook'
    _process_url = '/payment/payu/process'
    _cancel_url = '/payment/payu/cancel'

    # --------------------------
    # WEBHOOK (SERVER TO SERVER)
    # --------------------------
    @http.route(_webhook_url, type='http', auth='public', methods=['POST'], csrf=False)
    def payu_webhook(self, **kwargs):
        _logger.info("PayU Webhook received: %s", kwargs)

        reference = kwargs.get('udf2')

        if not reference:
            _logger.error("Missing reference (udf2) in webhook")
            return "Missing reference"

        _logger.info("Searching transaction with reference: %s", reference)

        tx = request.env[PAYMENT_TRANSACTION_MODEL].sudo().search(
            [('reference', '=', reference)], limit=1
        )

        if not tx:
            _logger.error("Transaction not found for reference %s", reference)
            return "Transaction not found"

        try:
            tx._process_notification_data(kwargs)
        except Exception as e:
            _logger.exception("Error processing webhook: %s", e)
            return "Error"

        return "OK"

    # --------------------------
    # RETURN / REDIRECT
    # --------------------------
    @http.route(_process_url, type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def payu_process(self, **kwargs):
        _logger.info("PayU redirection response received: %s", kwargs)

        reference = kwargs.get('udf2')

        if not reference:
            _logger.error("Missing reference (udf2) in return")
            return request.redirect('/payment/status')

        _logger.info("Searching transaction with reference: %s", reference)

        tx = request.env[PAYMENT_TRANSACTION_MODEL].sudo().search(
            [('reference', '=', reference)], limit=1
        )

        if not tx:
            _logger.error("Transaction not found for reference %s", reference)
            return request.redirect('/payment/status')

        try:
            tx._process_notification_data(kwargs)
        except Exception as e:
            _logger.exception("Error processing return: %s", e)

        return request.redirect('/payment/status')

    # --------------------------
    # CANCEL
    # --------------------------
    @http.route(_cancel_url, type='http', auth='public', methods=['GET', 'POST'], csrf=False, save_session=False)
    def payu_cancel(self, **kwargs):
        txn_ref = kwargs.get('txn_ref')

        TERMINAL_STATES = ('done', 'cancel', 'error', 'authorized')

        if not txn_ref:
            _logger.warning("Missing txn_ref in cancel")
            return request.redirect('/payment/status')

        tx = request.env[PAYMENT_TRANSACTION_MODEL].sudo().search(
            [('reference', '=', txn_ref)], limit=1
        )

        if not tx:
            _logger.warning("Transaction not found for %s", txn_ref)
            return request.redirect('/payment/status')

        if tx.state in TERMINAL_STATES:
            _logger.info("Transaction already terminal: %s", txn_ref)
            return request.redirect('/payment/status')

        _logger.info("Canceling transaction %s", txn_ref)
        tx._set_canceled()

        return request.redirect('/payment/status')