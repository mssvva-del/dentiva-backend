"""Clinic Terms + Business Associate Agreement — the click-through document a
clinic accepts during onboarding (ONB-BAA).

⚠️ DRAFT — legal review required before the FIRST live clinic. This encodes the
risk-shifting clauses from dentiva-docs/_docs/LEGAL_COMPLIANCE_US.md (§1 HIPAA
BA duties, §2 TCPA consent warranty, §4 recording responsibility, §6 "not a
healthcare provider" + liability). A US healthcare/TCPA attorney must review the
final wording; this text exists so the ACCEPTANCE MACHINERY (versioning, e-sign
capture, go-live gate) is built and tested now.

Versioning contract: bump BAA_VERSION whenever the text materially changes. A
practice's stored acceptance is tied to the version it signed; the go-live gate
(and later, a re-acceptance prompt) compares against the CURRENT version, so a
clinic on an outdated signature is treated as not-accepted for new terms.
"""

from __future__ import annotations

# Bump on any material change to BAA_DOCUMENT (see versioning contract above).
BAA_VERSION = "2026-07-draft-1"

BAA_DOCUMENT = """\
DENTOVOX CLINIC TERMS & BUSINESS ASSOCIATE AGREEMENT
Version 2026-07-draft-1  —  DRAFT, pending legal review

By accepting, the dental practice ("Clinic") and Dentovox ("Dentovox") agree:

1. HIPAA / Business Associate.
   Dentovox acts as a Business Associate of the Clinic (a Covered Entity) under
   45 CFR Parts 160 and 164. Dentovox will: use and disclose Protected Health
   Information (PHI) only as permitted to perform the services or as required by
   law; apply the administrative, physical, and technical safeguards of the
   Security Rule; report any Breach of unsecured PHI to the Clinic without
   unreasonable delay and no later than the period stated in this agreement;
   ensure its subcontractors that handle PHI agree in writing to the same
   restrictions; and, on termination, return or destroy PHI where feasible.

2. Patient Consent (TCPA) — Clinic warranty.
   The Clinic represents and warrants that every phone number it provides or
   makes available for outbound contact (calls or texts, including reactivation)
   was obtained directly from the patient and carries the patient's prior
   express consent to be contacted for treatment/appointment purposes. The
   Clinic will NOT request or configure promotional/marketing content, which
   requires prior express written consent the Clinic separately represents it
   does not have. The Clinic indemnifies Dentovox against claims arising from a
   breach of this Section.

3. Call Recording.
   Calls may be recorded and disclosed to the caller at the start of each call.
   The Clinic is responsible for any request to disable this disclosure and
   assumes the associated risk in all-party-consent jurisdictions.

4. Not a Healthcare Provider.
   Dentovox provides communication and scheduling software and an AI assistant.
   Dentovox does not provide medical or dental advice, diagnosis, or treatment.
   The Clinic is responsible for the clinical content of any scripts and for
   verifying appointments and records. The AI may make recognition or
   transcription errors; the Clinic will verify critical records.

5. Liability.
   To the maximum extent permitted by law, Dentovox's aggregate liability is
   limited to the fees paid by the Clinic in the twelve (12) months preceding
   the claim; neither party is liable for indirect or consequential damages.

Acceptance is recorded with the signer's name, title, timestamp, and IP address
as an electronic signature under the ESIGN Act.
"""


def current_baa() -> dict:
    """The current document + version for display in onboarding."""
    return {"version": BAA_VERSION, "text": BAA_DOCUMENT}
