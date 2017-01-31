from asyncbb.handlers import BaseHandler
from asyncbb.errors import JSONHTTPError
from asyncbb.database import DatabaseMixin
from asyncbb.ethereum.mixin import EthereumMixin
from asyncbb.ethereum.client import JsonRPCError
from asyncbb.redis import RedisMixin
from tokenbrowser.utils import (
    validate_address, parse_int, validate_signature
)
from tokenbrowser.utils import data_decoder, data_encoder
from tokenbrowser.tx import (
    DEFAULT_STARTGAS, DEFAULT_GASPRICE, create_transaction,
    encode_transaction, decode_transaction, is_transaction_signed,
    signature_from_transaction, add_signature_to_transaction
)
from tokenservices.handlers import RequestVerificationMixin

import logging
logging.basicConfig()
log = logging.getLogger("token.log")

class BalanceMixin:

    async def get_balances(self, eth_address, ignore_pending_recieved=False):
        """Gets the confirmed balance of the eth address from the ethereum network
        and adjusts the value based off any pending transactions.

        The option to ignore the pending recieved transactions is used to
        prevent the server from allowing the user to create transactions based on
        a potential balance that may not be available yet
        """
        confirmed_balance = await self.eth.eth_getBalance(eth_address)

        async with self.db:
            pending_sent = await self.db.fetch(
                "SELECT value, estimated_gas_cost FROM transactions WHERE confirmed IS NULL AND from_address = $1",
                eth_address)

        if pending_sent and len(pending_sent) > 0:
            pending_sent = sum(int(p['value']) + int(p['estimated_gas_cost']) for p in pending_sent)
        else:
            pending_sent = 0

        if ignore_pending_recieved is False:
            async with self.db:
                pending_recieved = await self.db.fetch(
                    "SELECT value FROM transactions WHERE confirmed IS NULL AND to_address = $1",
                    eth_address)
            if pending_recieved and len(pending_recieved) > 0:
                pending_recieved = sum(int(p['value']) for p in pending_recieved)
            else:
                pending_recieved = 0
        else:
            pending_recieved = 0

        balance = confirmed_balance + pending_recieved - pending_sent

        return confirmed_balance, balance

class BalanceHandler(BalanceMixin, DatabaseMixin, EthereumMixin, BaseHandler):

    async def get(self, address):

        if not validate_address(address):
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_address', 'message': 'Invalid Address'}]})

        confirmed, unconfirmed = await self.get_balances(address)

        self.write({
            "confirmed_balance": hex(confirmed),
            "unconfirmed_balance": hex(unconfirmed)
        })

class TransactionSkeletonHandler(EthereumMixin, RedisMixin, BaseHandler):

    async def post(self):

        if 'value' not in self.json or 'to' not in self.json or 'from' not in self.json:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        value = self.json['value']
        to_address = self.json['to']
        from_address = self.json['from']

        if not validate_address(from_address):
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_from_address', 'message': 'Invalid From Address'}]})

        if not validate_address(to_address):
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_to_address', 'message': 'Invalid To Address'}]})

        value = parse_int(value)

        if not value:
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_value', 'message': 'Invalid Value'}]})

        # check optional arguments

        nonce = self.json.get('nonce')
        if nonce is None:
            # check cache for nonce
            nonce = self.redis.get("nonce:{}".format(from_address))
            if nonce:
                nonce = int(nonce)
            # get the network's value too
            nw_nonce = await self.eth.eth_getTransactionCount(from_address)
            if nonce is None or nw_nonce > nonce:
                # if not cached, or the cached value is lower than
                # the network value, use the network value!
                nonce = nw_nonce
        else:
            nonce = parse_int(nonce)
            if nonce is None:
                raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_nonce', 'message': 'Invalid Nonce'}]})

        gas = self.json.get('gas')
        if gas is None:
            gas = DEFAULT_STARTGAS
        else:
            gas = parse_int(gas)
            if gas is None:
                raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_gas', 'message': 'Invalid Gas'}]})

        gas_price = self.json.get('gas_price')
        if gas_price is None:
            gas_price = DEFAULT_GASPRICE
        else:
            gas_price = parse_int(gas_price)
            if gas_price is None:
                raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_gas_price', 'message': 'Invalid Gas Price'}]})

        tx = create_transaction(nonce=nonce, gasprice=gas_price, startgas=gas,
                                to=to_address, value=value)

        transaction = encode_transaction(tx)

        self.write({
            "tx_data": {
                "nonce": hex(nonce),
                "from": from_address,
                "to": to_address,
                "value": hex(value),
                "startGas": hex(gas),
                "gasPrice": hex(gas_price)
            },
            "tx": transaction
        })

class SendTransactionHandler(BalanceMixin, EthereumMixin, DatabaseMixin, RedisMixin, RequestVerificationMixin, BaseHandler):

    async def post(self):

        if self.is_request_signed():
            sender_token_id = self.verify_request()
        else:
            # this is an anonymous transaction
            sender_token_id = None

        payload = self.json

        if 'tx' not in payload:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        try:
            tx = decode_transaction(payload['tx'])
        except:
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_transaction', 'message': 'Invalid Transaction'}]})

        if is_transaction_signed(tx):

            if 'signature' in payload:

                tx_sig = signature_from_transaction(tx)
                if tx_sig != payload['signature']:

                    raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_signature', 'message': 'Invalid Signature'}]})
        else:

            if 'signature' not in payload:
                raise JSONHTTPError(400, body={'errors': [{'id': 'missing_signature', 'message': 'Missing Signature'}]})

            signature = payload['signature']

            if not validate_signature(signature):
                raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_signature', 'message': 'Invalid Signature'}]})

            try:
                signature = data_decoder(signature)
            except Exception:
                raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_signature', 'message': 'Invalid Signature'}]})

            add_signature_to_transaction(tx, signature)

        from_address = data_encoder(tx.sender)
        to_address = data_encoder(tx.to)

        # make sure the account has enough funds for the transaction
        network_balance, balance = await self.get_balances(from_address, ignore_pending_recieved=True)

        log.info("Attempting to send transaction\n{} -> {}\nValue: {} + {} (gas) * {} (startgas) = {}\nSender's Balance {} ({} unconfirmed)".format(
            from_address, to_address, tx.value, tx.startgas, tx.gasprice, tx.value + (tx.startgas * tx.gasprice), network_balance, balance))

        if balance < (tx.value + (tx.startgas * tx.gasprice)):
            raise JSONHTTPError(400, body={'errors': [{'id': 'insufficient_funds', 'message': 'Insufficient Funds'}]})

        # validate the nonce
        c_nonce = self.redis.get("nonce:{}".format(from_address))
        if c_nonce:
            c_nonce = int(c_nonce)
        # get the network's value too
        nw_nonce = await self.eth.eth_getTransactionCount(from_address)
        if c_nonce is None or nw_nonce > c_nonce:
            c_nonce = nw_nonce

        if tx.nonce < c_nonce:
            raise JSONHTTPError(400, body={'errors': [{'id': 'invalid_nonce', 'message': 'Provided nonce is too low'}]})
        # NOTE: since posting a nonce that is higher is valid we don't throw an error if it is much higher.
        # However, the ethereum node wont broadcast a transaction to the network until the nonce values between
        # it and the network value are filled in.

        # send the transaction to the network
        try:
            tx_encoded = encode_transaction(tx)
            tx_hash = await self.eth.eth_sendRawTransaction(tx_encoded)
        except JsonRPCError as e:
            raise JSONHTTPError(500, body={'errors': [{'id': 'unexpected_error',
                                                       'message': 'An error occured communicating with the ethereum network, try again later'}]})

        # cache nonce
        self.redis.set("nonce:{}".format(from_address), tx.nonce + 1)
        # add tx to database
        async with self.db:
            await self.db.execute(
                "INSERT INTO transactions (transaction_hash, from_address, to_address, value, estimated_gas_cost, sender_token_id) VALUES ($1, $2, $3, $4, $5, $6)",
                tx_hash, from_address, to_address, str(tx.value), str(tx.startgas * tx.gasprice), sender_token_id)
            await self.db.commit()

        self.write({
            "tx_hash": tx_hash
        })

class TransactionHandler(EthereumMixin, BaseHandler):

    async def get(self, tx_hash):

        tx = await self.eth.eth_getTransactionByHash(tx_hash)
        if tx is None:
            self.set_status(404)
        self.write({
            "tx": tx
        })

class TransactionNotificationRegistrationHandler(RequestVerificationMixin, DatabaseMixin, BaseHandler):

    async def post(self):

        token_id = self.verify_request()
        payload = self.json

        if 'addresses' not in payload or len(payload['addresses']) == 0:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        addresses = payload['addresses']

        insert_args = []
        for address in addresses:
            if not validate_address(address):
                raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})
            insert_args.extend([token_id, address])

        async with self.db:

            await self.db.execute(
                "INSERT INTO notification_registrations VALUES {} ON CONFLICT DO NOTHING".format(
                    ', '.join('(${}, ${})'.format((i * 2) + 1, (i * 2) + 2) for i, _ in enumerate(addresses))),
                *insert_args)

            await self.db.commit()

        self.set_status(204)

class TransactionNotificationDeregistrationHandler(RequestVerificationMixin, DatabaseMixin, BaseHandler):

    async def post(self):

        token_id = self.verify_request()
        payload = self.json

        if 'addresses' not in payload or len(payload['addresses']) == 0:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        addresses = payload['addresses']

        for address in addresses:
            if not validate_address(address):
                raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        async with self.db:

            await self.db.execute(
                "DELETE FROM notification_registrations WHERE token_id = $1 AND ({})".format(
                    ' OR '.join('eth_address = ${}'.format(i + 2) for i, _ in enumerate(addresses))),
                token_id, *addresses)

            await self.db.commit()

        self.set_status(204)

class PNRegistrationHandler(RequestVerificationMixin, DatabaseMixin, BaseHandler):

    async def post(self, service):

        token_id = self.verify_request()
        payload = self.json

        if 'registration_id' not in payload:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        # TODO: registration id verification

        async with self.db:

            await self.db.execute(
                "INSERT INTO push_notification_registrations (service, registration_id, token_id) VALUES ($1, $2, $3) ON CONFLICT (service, registration_id) DO UPDATE SET token_id = $3",
                service, payload['registration_id'], token_id)

            await self.db.commit()

        self.set_status(204)

class PNDeregistrationHandler(RequestVerificationMixin, DatabaseMixin, BaseHandler):

    async def post(self, service):

        token_id = self.verify_request()
        payload = self.json

        if 'registration_id' not in payload:
            raise JSONHTTPError(400, body={'errors': [{'id': 'bad_arguments', 'message': 'Bad Arguments'}]})

        # TODO: registration id verification

        async with self.db:

            await self.db.execute(
                "DELETE FROM push_notification_registrations WHERE service = $1 AND registration_id = $2 AND token_id = $3",
                service, payload['registration_id'], token_id)

            await self.db.commit()

        self.set_status(204)
