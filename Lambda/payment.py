# -*- coding: utf-8 -*-
"""
Lambda: payment

But: déclencher le paiement APRÈS signature du contrat.
- Vérifie que le contrat est signé (ContractsTable)
- Crée un enregistrement de paiement (PaymentsTable)
- Si provider=STRIPE:
    * PaymentIntent (si paymentMethodId fourni) ou Checkout Session
    * renvoie l'URL de paiement (si checkout) ou l'état immédiat
- Si provider=MOCK: simule un paiement (pour démo)

Entrée (event exemple):
{
  "contractId": "CTR-2025-001",
  "client": {"id":"C12345", "email":"amina@example.com", "name":"Amina"},
  "amount": 199.00,
  "currency": "MAD",
  "provider": "STRIPE",          # STRIPE | MOCK (défaut: MOCK)
  "paymentMethodId": null,       # optionnel (paiement direct)
  "successUrl": "https://app.ecoia/success",
  "cancelUrl": "https://app.ecoia/cancel"
}
"""
import os
import json
import time
import uuid
from decimal import Decimal
from typing import Any, Dict

import boto3
import urllib.parse
import urllib.request

# --- Stripe (appel HTTP sans dépendances) ---
STRIPE_API = "https://api.stripe.com/v1"

def _stripe_request(path: str, secret_key: str, params: Dict[str, Any]):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        STRIPE_API + path,
        data=data,
        headers={"Authorization": f"Bearer {secret_key}"}
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)

# --- AWS clients ---
ddb = boto3.resource('dynamodb')
contracts_table = ddb.Table(os.environ['CONTRACTS_TABLE'])
payments_table  = ddb.Table(os.environ['PAYMENTS_TABLE'])

def _amount_to_minor(amount: float, currency: str) -> int:
    # 2 décimales par défaut (adapter pour JPY, etc.)
    return int(Decimal(str(amount)) * 100)

def _get_contract(contract_id: str) -> Dict[str, Any]:
    r = contracts_table.get_item(Key={"pk": f"CONTRACT#{contract_id}", "sk": "META"})
    return r.get('Item', {})

def _create_payment_record(contract_id: str, client: Dict[str, Any],
                           amount: float, currency: str, provider: str) -> str:
    pid = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    payments_table.put_item(Item={
        'pk': f'PAYMENT#{pid}',
        'sk': 'META',
        'contractId': contract_id,
        'clientId': client.get('id'),
        'amount': float(amount),
        'currency': currency.upper(),
        'provider': provider,
        'status': 'PENDING',
        'createdAt': int(time.time())
    })
    return pid

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    provider    = (event.get('provider') or os.environ.get('PROVIDER') or 'MOCK').upper()
    contract_id = event.get('contractId')
    if not contract_id:
        return {"ok": False, "error": "contractId requis"}

    contract = _get_contract(contract_id)
    if not contract:
        return {"ok": False, "error": "contrat introuvable"}
    if contract.get('status') not in ('SIGNED', 'SIGN_COMPLETED', 'SIGNED_OK'):
        return {"ok": False, "error": f"contrat non signé (status={contract.get('status')})"}

    client   = event.get('client') or {}
    amount   = float(event.get('amount', 0))
    currency = (event.get('currency') or 'MAD').upper()
    if amount <= 0:
        return {"ok": False, "error": "amount invalide"}

    payment_id = _create_payment_record(contract_id, client, amount, currency, provider)

    if provider == 'STRIPE':
        secret = os.environ.get('STRIPE_SECRET_KEY')
        if not secret:
            return {"ok": False, "error": "STRIPE_SECRET_KEY manquant"}

        pm          = event.get('paymentMethodId')
        description = f"Contrat {contract_id}"
        metadata    = {'contractId': contract_id, 'paymentId': payment_id, 'clientId': client.get('id', '')}

        try:
            if pm:
                # Paiement direct (off_session si PM enregistré)
                intent = _stripe_request('/payment_intents', secret, {
                    'amount': _amount_to_minor(amount, currency),
                    'currency': currency.lower(),
                    'payment_method': pm,
                    'confirm': 'true',
                    'off_session': 'true',
                    'description': description,
                    **{f'metadata[{k}]': v for k, v in metadata.items()}
                })
                status = intent.get('status')
                prov_id = intent.get('id')
                payments_table.update_item(
                    Key={'pk': f'PAYMENT#{payment_id}', 'sk': 'META'},
                    UpdateExpression='SET #s=:s, providerRef=:r',
                    ExpressionAttributeNames={'#s':'status'},
                    ExpressionAttributeValues={':s': status.upper(), ':r': prov_id}
                )
                return {"ok": True, "payment":
                        {"paymentId": payment_id, "status": status.upper(), "provider": provider}}
            else:
                # Checkout Session (retourne l’URL à afficher/envoyer)
                success_url = event.get('successUrl') or 'https://example.org/success'
                cancel_url  = event.get('cancelUrl')  or 'https://example.org/cancel'
                params = {
                    'mode': 'payment',
                    'success_url': success_url,
                    'cancel_url': cancel_url,
                    'customer_email': client.get('email', ''),
                    'line_items[0][price_data][currency]': currency.lower(),
                    'line_items[0][price_data][product_data][name]': description,
                    'line_items[0][price_data][unit_amount]': _amount_to_minor(amount, currency),
                    'line_items[0][quantity]': 1,
                    **{f'metadata[{k}]': v for k, v in metadata.items()}
                }
                session = _stripe_request('/checkout/sessions', secret, params)
                url = session.get('url')
                sid = session.get('id')
                payments_table.update_item(
                    Key={'pk': f'PAYMENT#{payment_id}', 'sk': 'META'},
                    UpdateExpression='SET providerRef=:r, checkoutUrl=:u',
                    ExpressionAttributeValues={':r': sid, ':u': url}
                )
                return {"ok": True, "payment":
                        {"paymentId": payment_id, "status": "PENDING", "provider": provider, "checkoutUrl": url}}
        except Exception as e:
            payments_table.update_item(
                Key={'pk': f'PAYMENT#{payment_id}', 'sk': 'META'},
                UpdateExpression='SET #s=:s, error=:e',
                ExpressionAttributeNames={'#s':'status'},
                ExpressionAttributeValues={':s': 'FAILED', ':e': str(e)}
            )
            return {"ok": False, "error": f"StripeError: {str(e)}"}

    # Provider MOCK
    payments_table.update_item(
        Key={'pk': f'PAYMENT#{payment_id}', 'sk': 'META'},
        UpdateExpression='SET #s=:s, providerRef=:r',
        ExpressionAttributeNames={'#s':'status'},
        ExpressionAttributeValues={':s': 'PAID', ':r': 'MOCK-TXN'}
    )
    return {"ok": True, "payment": {"paymentId": payment_id, "status": "PAID", "provider": provider}}
