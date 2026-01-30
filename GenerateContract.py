import os
import io
import json
import base64
import datetime
import logging
from typing import Optional, Tuple

import boto3

# ========= Config & clients AWS =========
logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
ses = boto3.client("ses")

BUCKET_NAME = os.environ.get("BUCKET_NAME","energy-contracts-pdf-prod")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL","eco.ia@capgemini.com")             
PRESIGNED_TTL = int(os.environ.get("PRESIGNED_TTL", "900"))
COMPANY_NAME = os.environ.get("COMPANY_NAME", "EcoIA")

# Logo par défaut (optionnel)
DEFAULT_LOGO_BUCKET = os.environ.get("LOGO_S3_BUCKET","energy-contracts-pdf-prod")
DEFAULT_LOGO_KEY = os.environ.get("LOGO_S3_KEY","brand/logo.jpg")


# ========= Encodage & échappement texte PDF =========

def _to_pdf_ansi(s: str) -> str:
    """
    Normalise les caractères non-Latin-1 (Unicode) afin que l'encodage 'latin-1'
    du flux PDF ne plante pas. Conserve les accents FR (éèà...).
    """
    if s is None:
        return ""
    repl = {
        "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2015": "-", "\u2212": "-",
        "\u2018": "'", "\u2019": "'", "\u201A": ",",
        "\u201C": '"', "\u201D": '"', "\u201E": '"',
        "\u2026": "...",
        "\u00A0": " ", "\u202F": " ", "\u2009": " ", "\u2007": " ",
        "\u2002": " ", "\u2003": " ", "\u200A": " ", "\u200B": "", "\u2060": ""
    }
    out = []
    for ch in s:
        if ch in repl:
            out.append(repl[ch]); continue
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)

def _pdf_escape_text(s: str) -> str:
    r"""
    Échappe les caractères spéciaux PDF (\, (, )) après normalisation ANSI.
    IMPORTANT : appeler _to_pdf_ansi avant !
    """
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# ========= PDF utils (pur Python, sans libs) =========

def _jpeg_size(jpeg_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Retourne (width, height) d'un JPEG en lisant le segment SOF."""
    data = jpeg_bytes
    if not (len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8):
        return None
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 7 > len(data):
                return None
            length = (data[i] << 8) + data[i+1]
            if i + length > len(data):
                return None
            height = (data[i+3] << 8) + data[i+4]
            width  = (data[i+5] << 8) + data[i+6]
            return (width, height)
        else:
            if i + 2 > len(data):
                return None
            seglen = (data[i] << 8) + data[i+1]
            i += 2 + (seglen - 2)
    return None

class Pdf:
    """Assembleur PDF minimal avec numérotation d'objets automatique."""
    def __init__(self):
        self.header = b"%PDF-1.7\n"
        self.obj_bodies: list[bytes] = []

    def add(self, body_without_id: bytes) -> int:
        """
        Ajoute un objet SANS l'en-tête 'n 0 obj'.
        Retourne l'id (1-based) de l'objet tel qu'il sera construit.
        """
        self.obj_bodies.append(body_without_id)
        return len(self.obj_bodies)

    def build(self) -> bytes:
        # Construire les objets avec ids
        objects = []
        for i, body in enumerate(self.obj_bodies, start=1):
            obj = f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
            objects.append(obj)
        # Offsets
        offsets = []
        current = len(self.header)
        for obj in objects:
            offsets.append(current)
            current += len(obj)
        # xref
        xref = [b"0000000000 65535 f \n"]
        for off in offsets:
            xref.append(f"{off:010d} 00000 n \n".encode("latin-1"))
        xref_bytes = b"xref\n0 " + str(len(objects)+1).encode("latin-1") + b"\n" + b"".join(xref)
        # trailer
        startxref = len(self.header) + sum(len(o) for o in objects)
        trailer = (
            b"trailer\n<< /Size " + str(len(objects)+1).encode("latin-1") +
            b" /Root 1 0 R >>\nstartxref\n" + str(startxref).encode("latin-1") + b"\n%%EOF\n"
        )
        return self.header + b"".join(objects) + xref_bytes + trailer


def build_contract_pdf(payload: dict, logo_jpeg: Optional[bytes]) -> bytes:
    """
    Génère un PDF A4 : logo (JPEG), titre, blocs Client/Offre/Conditions, signatures, pied de page.
    100% sans dépendances externes.
    """
    def wrap_lines(text: str, max_chars: int = 95) -> list[str]:
        text = _to_pdf_ansi(text or "")
        lines = []
        for para in text.splitlines():
            p = para.strip()
            while len(p) > max_chars:
                cut = p.rfind(" ", 0, max_chars)
                if cut < 40:
                    cut = max_chars
                lines.append(p[:cut].strip())
                p = p[cut:].strip()
            if p:
                lines.append(p)
        if not lines:
            lines.append("")
        return lines

    def tj_line(s: str) -> str:
        s = _pdf_escape_text(_to_pdf_ansi(s))
        return f"({s}) Tj\n"

    pdf = Pdf()

    # 1) Catalog (placeholder vers /Pages)
    catalog_id = pdf.add(b"<< /Type /Catalog /Pages 2 0 R >>")

    # 2) Pages (placeholder; on mettra le /Kids après /Page)
    pages_id = pdf.add(b"<< /Type /Pages /Kids [] /Count 1 >>")

    # 3) Font Helvetica **avec WinAnsiEncoding** (clé du correctif)
    font_id = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

    # 4) Image (logo JPEG) optionnelle
    img_id = None
    img_width = img_height = None
    if logo_jpeg:
        wh = _jpeg_size(logo_jpeg)
        if wh:
            img_width, img_height = wh
            img_dict = (
                f"<< /Type /XObject /Subtype /Image /Width {img_width} /Height {img_height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_jpeg)} >>\n"
            ).encode("latin-1")
            img_stream = img_dict + b"stream\n" + logo_jpeg + b"\nendstream"
            img_id = pdf.add(img_stream)

    # 5) Contents (texte + dessin) en stream
    left = 40
    top_y = 812
    cursor_y = top_y

    buf = io.StringIO()

    # On ouvre un état global (optionnel) et on met le texte en noir
    buf.write("q\n")
    buf.write("0 0 0 rg\n")    # fill color noir

    # --- Logo encapsulé dans son propre état graphique ---
    if img_id and img_width and img_height:
        target_w = 110.0
        scale = target_w / float(img_width)
        target_h = img_height * scale
        x = float(left)
        y = float(842 - 30 - target_h)

        buf.write("q\n")  # état local pour le logo
        buf.write(f"{target_w:.2f} 0 0 {target_h:.2f} {x:.2f} {y:.2f} cm\n")
        buf.write("/Im1 Do\n")
        buf.write("Q\n")  # restaure CTM -> le texte ne sera PAS transformé par 'cm'
        cursor_y = y - 20
    else:
        cursor_y = top_y

    # Après avoir restauré l'état, on remet explicitement le texte en noir
    buf.write("0 0 0 rg\n")

    # Titre
    title = f"Contrat d'Énergie — {payload.get('contratId','—')}"
    buf.write("BT\n/F1 20 Tf\n")
    buf.write(f"{left} {cursor_y:.2f} Td\n")
    buf.write(tj_line(title))
    buf.write("ET\n")
    cursor_y -= 28

    # Section Client
    buf.write("BT\n/F1 12 Tf\n")
    buf.write(f"{left} {cursor_y:.2f} Td\n")
    buf.write(tj_line("Informations Client"))
    buf.write("ET\n")
    cursor_y -= 16

    client = payload.get("client", {})
    client_lines = [
        f"Nom : {client.get('nom','')}",
        f"Prénom : {client.get('prenom','')}",
        f"Adresse : {client.get('adresse','')}",
        f"E-mail : {client.get('email','')}",
    ]
    buf.write("BT\n/F1 11 Tf\n")
    buf.write(f"{left} {cursor_y:.2f} Td\n")
    buf.write("14 TL\n")
    used = 0
    for ln in client_lines:
        for wln in wrap_lines(ln, 95):
            buf.write(tj_line(wln)); buf.write("T*\n"); used += 1
    buf.write("ET\n")
    cursor_y -= 14 * (used + 1)

    # Section Offre
    buf.write("BT\n/F1 12 Tf\n")
    buf.write(f"{left} {cursor_y:.2f} Td\n")
    buf.write(tj_line("Offre choisie"))
    buf.write("ET\n")
    cursor_y -= 16

    offre = payload.get("offre", {})
    offre_lines = [
        f"Nom de l'offre : {offre.get('nomOffre') or offre.get('offreChoisie','')}",
        f"Prix unitaire : {offre.get('prixUnitaire','—')} {offre.get('devise','EUR')}/kWh",
        f"Détails : {offre.get('details','')}",
    ]
    buf.write("BT\n/F1 11 Tf\n")
    buf.write(f"{left} {cursor_y:.2f} Td\n")
    buf.write("14 TL\n")
    used = 0
    for ln in offre_lines:
        for wln in wrap_lines(ln, 95):
            buf.write(tj_line(wln)); buf.write("T*\n"); used += 1
    buf.write("ET\n")
    cursor_y -= 14 * (used + 1)

    # Conditions
    conditions_text = payload.get("conditions", "")
    if conditions_text:
        buf.write("BT\n/F1 12 Tf\n")
        buf.write(f"{left} {cursor_y:.2f} Td\n")
        buf.write(tj_line("Conditions"))
        buf.write("ET\n")
        cursor_y -= 16

        buf.write("BT\n/F1 11 Tf\n")
        buf.write(f"{left} {cursor_y:.2f} Td\n")
        buf.write("14 TL\n")
        lines = wrap_lines(str(conditions_text), 95)
        for wln in lines:
            buf.write(tj_line(wln)); buf.write("T*\n")
        buf.write("ET\n")
        cursor_y -= 14 * (len(lines) + 1)

    # Signatures
    sig_y = max(cursor_y - 40, 120)
    buf.write("1 w 0 0 0 RG 0 0 0 rg\n")
    buf.write(f"{left} {sig_y:.2f} m {left+200:.2f} {sig_y:.2f} l S\n")
    buf.write(f"{left+240:.2f} {sig_y:.2f} m {left+440:.2f} {sig_y:.2f} l S\n")
    buf.write("BT\n/F1 10 Tf\n")
    buf.write(f"{left} {sig_y+6:.2f} Td\n")
    buf.write(tj_line("Signature Client"))
    buf.write("ET\n")
    buf.write("BT\n/F1 10 Tf\n")
    buf.write(f"{left+240:.2f} {sig_y+6:.2f} Td\n")
    buf.write(tj_line("Signature Fournisseur"))
    buf.write("ET\n")

    # Pied de page
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    footer = f"Généré le {now_utc} — {COMPANY_NAME}"
    buf.write("BT\n/F1 9 Tf\n")
    buf.write(f"{left} 40 Td\n")
    buf.write(tj_line(footer))
    buf.write("ET\n")

    buf.write("Q\n")  # restore graphics state global

    stream_bytes = buf.getvalue().encode("latin-1")
    contents_id = pdf.add(
        f"<< /Length {len(stream_bytes)} >>\n".encode("latin-1") +
        b"stream\n" + stream_bytes + b"\nendstream"
    )

    # 6) Page (références resources et contents)
    resources = f"<< /Font << /F1 {font_id} 0 R >>".encode("latin-1")
    if img_id:
        resources += f" /XObject << /Im1 {img_id} 0 R >>".encode("latin-1")
    resources += b" >>"

    page_id = pdf.add(
        b"<< /Type /Page /Parent " + f"{pages_id} 0 R".encode("latin-1") +
        b" /MediaBox [0 0 595 842] " +
        b" /Resources " + resources +
        b" /Contents " + f"{contents_id} 0 R".encode("latin-1") +
        b" >>"
    )

    # 7) Mettre à jour /Pages (/Kids)
    pdf.obj_bodies[pages_id - 1] = (
        b"<< /Type /Pages /Kids [" + f"{page_id} 0 R".encode("latin-1") + b"] /Count 1 >>"
    )

    # 8) Mettre à jour /Catalog (pointe vers /Pages)
    pdf.obj_bodies[catalog_id - 1] = (
        b"<< /Type /Catalog /Pages " + f"{pages_id} 0 R".encode("latin-1") + b" >>"
    )

    return pdf.build()


# ========= I/O helpers =========

def _parse_event(event) -> dict:
    """Retourne le payload JSON depuis API GW (body) ou invocation directe."""
    if isinstance(event, dict) and "body" in event:
        body = event["body"]
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        try:
            return json.loads(body or "{}")
        except Exception as e:
            logger.error(f"JSON invalide: {e}")
            return {}
    return event if isinstance(event, dict) else {}

def _load_logo_bytes(payload: dict) -> Optional[bytes]:
    """Charge le logo : Base64, S3 explicite, ou S3 par défaut (JPEG recommandé)."""
    logo = payload.get("logo", {})
    if isinstance(logo, dict) and "logoBase64" in logo:
        try:
            return base64.b64decode(logo["logoBase64"])
        except Exception as e:
            logger.warning(f"Logo Base64 invalide: {e}")
    if isinstance(logo, dict) and logo.get("s3Bucket") and logo.get("s3Key"):
        try:
            obj = s3.get_object(Bucket=logo["s3Bucket"], Key=logo["s3Key"])
            return obj["Body"].read()
        except Exception as e:
            logger.warning(f"Lecture logo S3 (payload) impossible: {e}")
    if DEFAULT_LOGO_BUCKET and DEFAULT_LOGO_KEY:
        try:
            obj = s3.get_object(Bucket=DEFAULT_LOGO_BUCKET, Key=DEFAULT_LOGO_KEY)
            return obj["Body"].read()
        except Exception as e:
            logger.warning(f"Lecture logo S3 (env) impossible: {e}")
    return None

def _presign(bucket: str, key: str, ttl: int) -> str:
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl
    )

def _send_email_with_link(to_email: str, presigned_url: str, contrat_id: str) -> str:
    """Envoie un email via SES et retourne le MessageId (preuve d'acceptation SES)."""
    subject = f"Votre contrat d'énergie {contrat_id}"
    text_body = (
        f"Bonjour,\n\n"
        f"Votre contrat {contrat_id} est prêt.\n"
        f"Vous pouvez le télécharger ici (valable {PRESIGNED_TTL//60} min):\n{presigned_url}\n\n"
        f"Cordialement,\n{COMPANY_NAME}"
    )
    html_body = (
        f"<p>Bonjour,</p>"
        f"<p>Votre contrat <b>{contrat_id}</b> est prêt.</p>"
        f"<p><a href=\"{presigned_url}\">Télécharger le contrat</a> "
        f"(lien valable {PRESIGNED_TTL//60} min)</p>"
        f"<p>Cordialement,
{COMPANY_NAME}</p>"
    )
    resp = ses.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )
    msg_id = resp["MessageId"]
    logger.info(f"SES MessageId={msg_id} envoyé à {to_email}")
    return msg_id


def lambda_handler(event, context):
    if not BUCKET_NAME:
        return {"statusCode": 500, "body": json.dumps({"error": "BUCKET_NAME manquant"})}
    if not SENDER_EMAIL:
        return {"statusCode": 500, "body": json.dumps({"error": "SENDER_EMAIL manquant"})}

    payload = _parse_event(event)
    logger.info(f"Payload: {json.dumps(payload)[:1200]}")

    contrat_id = (payload.get("contratId") or f"CONTRAT-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}").strip()
    client_email = payload.get("client", {}).get("email")

    # 1) Logo (JPEG recommandé)
    logo_bytes = _load_logo_bytes(payload)

    # 2) PDF
    try:
        pdf_bytes = build_contract_pdf(payload, logo_bytes)
    except Exception as e:
        logger.exception("Erreur génération PDF")
        return {"statusCode": 500, "body": json.dumps({"error": "Erreur génération PDF", "details": str(e)})}

    # 3) S3
    year = datetime.datetime.utcnow().strftime("%Y")
    s3_key = f"contracts/{year}/{contrat_id}.pdf"
    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            Metadata={"contratId": contrat_id, "company": COMPANY_NAME},
        )
    except Exception as e:
        logger.exception("Erreur S3 put_object")
        return {"statusCode": 500, "body": json.dumps({"error": "Upload S3 KO", "details": str(e)})}

    # 4) URL présignée
    try:
        url = _presign(BUCKET_NAME, s3_key, PRESIGNED_TTL)
    except Exception as e:
        logger.exception("Erreur presign")
        url = ""

    # 5) Email SES
    ses_message_id = ""
    if client_email:
        try:
            ses_message_id = _send_email_with_link(client_email, url, contrat_id)
        except Exception as e:
            logger.exception("Erreur envoi email SES")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Contrat généré, email KO",
                    "bucket": BUCKET_NAME,
                    "key": s3_key,
                    "downloadUrl": url,
                    "email": client_email or "",
                    "sesError": str(e),
                }),
            }

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Contrat généré et stocké",
            "bucket": BUCKET_NAME,
            "key": s3_key,
            "downloadUrl": url,
            "email": client_email or "",
            "sesMessageId": ses_message_id
        }),
    }
