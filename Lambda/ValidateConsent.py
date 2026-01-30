"""
AWS Lambda: ValidateConsent

Responsabilité:
- Valider le consentement explicite du client (RGPD)
- Persister la preuve de consentement dans DynamoDB
- Retourner un résultat utilisable par Step Functions / API Gateway

Prérequis AWS:
- Table DynamoDB (CONSENTS_TABLE) avec clés partition/sortie: pk (S), sk (S)
- IAM: ddb:PutItem, ddb:GetItem
- Optionnel: CloudWatch Logs pour observabilité

Env vars:
- CONSENTS_TABLE: nom de la table DynamoDB
- RETENTION_YEARS: nombre d'années de rétention (par défaut 2)

Entrée (event):
{
  "requestId": "REQ-2025-12-24-ABC123",
  "clientId": "C12345",
  "consent": {
    "accepted": true,
    "versionText": "v1.3",
    "timestamp": "2025-12-24T13:59:42Z",
    "ip": "203.0.113.10",
    "userAgent": "Mozilla/5.0...",
    "locale": "fr-MA"
  }
}

Sortie (réussite):
{
  "ok": true,
  "consentStored": true,
  "hashProof": "sha256:...",
  "retentionUntil": "2027-12-24"
}

Sortie (échec):
{
  "ok": false,
  "error": "CONSENT_MISSING_OR_INVALID"
}
"""

import os
import json
import hashlib
from datetime import datetime, date
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

# Clients AWS
_dynamodb = boto3.resource("dynamodb")

# Constantes
DEFAULT_RETENTION_YEARS = int(os.environ.get("RETENTION_YEARS", "2"))
TABLE_NAME = os.environ.get("CONSENTS_TABLE", "")

# Exceptions applicatives
class BadRequest(Exception):
    pass


def _parse_iso_date(dt_str: str) -> date:
    """Parse 'YYYY-MM-DD' depuis un timestamp ISO 8601 (ex: 2025-12-24T13:59:42Z)."""
    try:
        # Supporte 'Z' et offsets
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.date()
    except Exception as e:
        raise BadRequest(f"Timestamp invalide: {dt_str}") from e


def _year_offset(d: date, years: int) -> date:
    """Ajoute un nombre d'années à une date en conservant jour/mois si possible."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Gérer 29 fév sur année non bissextile → 28 fév
        return d.replace(month=2, day=28, year=d.year + years)


def _hash_proof(client_id: str, ts_iso: str, version: str, request_id: str) -> str:
    """Construit une preuve immuable de consentement (hash SHA‑256)."""
    raw = f"{client_id}|{ts_iso}|{version}|{request_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    """Valide la structure minimale de l'événement et retourne un dict nettoyé."""
    if not isinstance(event, dict):
        raise BadRequest("Payload JSON requis")

    request_id = event.get("requestId")
    client_id = event.get("clientId")
    consent = event.get("consent") or {}

    if not request_id or not client_id:
        raise BadRequest("requestId et clientId sont requis")

    if not isinstance(consent, dict) or not consent.get("accepted"):
        raise BadRequest("Consentement explicite (accepted=true) requis")

    version = consent.get("versionText") or "v1"
    ts_iso = consent.get("timestamp")
    if not ts_iso:
        raise BadRequest("consent.timestamp requis (ISO 8601)")

    # Normalisation champ optionnels
    ip = consent.get("ip")
    ua = consent.get("userAgent")
    locale = consent.get("locale") or "fr"

    return {
        "requestId": request_id,
        "clientId": client_id,
        "consent": {
            "accepted": True,
            "versionText": version,
            "timestamp": ts_iso,
            "ip": ip,
            "userAgent": ua,
            "locale": locale,
            "channel": event.get("channel", "WEB")
        }
    }


def _put_item(table_name: str, item: Dict[str, Any]) -> None:
    table = _dynamodb.Table(table_name)
    table.put_item(Item=item)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handler AWS Lambda pour la validation et l'enregistrement du consentement."""
    if not TABLE_NAME:
        return {"ok": False, "error": "MISSING_ENV_CONSENTS_TABLE"}

    try:
        data = _validate_payload(event)
        req = data["requestId"]
        client_id = data["clientId"]
        consent = data["consent"]

        # Calculs
        ts_iso = consent["timestamp"]
        base_date = _parse_iso_date(ts_iso)
        retention_date = _year_offset(base_date, DEFAULT_RETENTION_YEARS)
        version = consent["versionText"]
        hash_proof = _hash_proof(client_id, ts_iso, version, req)

        # Enregistrement DynamoDB
        item = {
            "pk": f"CLIENT#{client_id}",
            "sk": f"CONSENT#{ts_iso}",
            "requestId": req,
            "accepted": True,
            "versionText": version,
            "timestamp": ts_iso,
            "ip": consent.get("ip"),
            "userAgent": consent.get("userAgent"),
            "locale": consent.get("locale"),
            "channel": consent.get("channel"),
            "hashProof": hash_proof,
            "retentionUntil": retention_date.isoformat(),
        }
        _put_item(TABLE_NAME, item)

        return {
            "ok": True,
            "consentStored": True,
            "hashProof": hash_proof,
            "retentionUntil": retention_date.isoformat(),
        }

    except BadRequest as br:
        return {"ok": False, "error": str(br)}
    except ClientError as ce:
        return {"ok": False, "error": f"DynamoDBError: {ce.response['Error']['Message']}"}
    except Exception as e:
        return {"ok": False, "error": f"UnexpectedError: {str(e)}"}
