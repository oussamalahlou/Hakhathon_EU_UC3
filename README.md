
# ECOIA – UC3 Energy & Utilities (Hackathon)

> **Traitement intelligent des demandes clients 100% digital** (énergie : gaz/électricité) – de la demande web jusqu’à la **signature** (DocuSign) et au **paiement après signature** (option).

---

## 🧭 Objectif

Construire un parcours client fluide et automatisé :

1. Le client saisit une demande (texte libre + champs clés) et dépose des justificatifs (PJ).
2. Le système **classe** et **vérifie** automatiquement (IA + OCR).
3. En cas d’anomalie ou faible confiance → **Human‑in‑the‑Loop** (Back‑office via SNS).
4. Si OK → le client donne son **consentement RGPD**.
5. Le système génère le contrat, lance la **signature électronique** et met à jour le statut.
6. (Optionnel) Paiement après signature.

---

## 🧱 Architecture AWS (vue technique)

### Services

- **Front** : Amazon **S3** (site statique) + **CloudFront** (CDN)
- **API** : **API Gateway**
- **Orchestration** : **Step Functions**
- **Compute** : **AWS Lambda**
- **IA** : **Amazon Bedrock** (LLM), **Amazon Textract** (OCR)
- **Data** : **DynamoDB** (statuts, consentements), **S3** (documents)
- **HITL** : **SNS** (notification back‑office)
- **Signature** : **DocuSign** (API) + **Webhook** (Event Hook)

---

## 🗺️ Schéma d’architecture (Mermaid)

> GitHub supporte Mermaid dans les README.

```mermaid
flowchart LR
  U[Utilisateur] -->|HTTPS| CF[CloudFront]
  CF --> S3WEB[(S3: site web statique)]
  U -->|POST /request| APIGW[API Gateway]
  APIGW -->|StartExecution| SFN[Step Functions]

  SFN --> C[Lambda: Classify]
  C -->|LLM| BR[Amazon Bedrock]
  C --> V[Lambda: Verify]

  V -->|LLM (option)| BR
  V -->|OCR pièces| TX[Amazon Textract]
  V --> DDB[(DynamoDB: demandes/statuts)]
  V --> S3DOC[(S3: pièces/contrats)]
  V -->|Si anomalie| SNS[SNS: Back‑Office (HITL)]
  V -->|Réponse| APIGW

  U -->|UI consentement| CF
  U -->|POST /consent| APIGW
  APIGW --> VC[Lambda: ValidateConsent]
  VC --> DDB
  VC --> GC[Lambda: GenerateContract]
  GC --> S3DOC
  GC --> SIG[Lambda: Signature]
  SIG -->|API| DS[DocuSign]
  DS -->|Webhook| WH[Webhook Lambda]
  WH --> DDB
  WH --> S3DOC

  %% option paiement
  WH --> PAY[Lambda: ProcessPayment (option)]
  PAY --> DDB
