"""Bounded PDF Oxide security operations with explicit verification limits."""

from __future__ import annotations

import hashlib
import io
import json
import math

from .core import JobError
from .oxide_runtime import native, open_source, require_runtime, write_bytes, write_json

MAX_SIGNATURES = 100
MAX_REGIONS = 100
MAX_DOCUMENT_PAGES = 100
MAX_REPORT_BYTES = 1024 * 1024
REDACTION_BLOCKED = (
    "PDF Oxide redaction is unavailable: the pinned engine can retain recoverable "
    "content beneath black rectangles. No redacted copy is published."
)


def field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide_security": {
        "label": "PDF Oxide encryption and signature inspection",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Inspect advisory permissions and existing signatures, or create an AES-256 "
            "encrypted/decrypted copy. Signature checks do not establish certificate trust "
            "or revocation status. Rewriting invalidates existing signatures. "
            "The pinned wheel's encrypted and decrypted rewrites currently fail independent "
            "checks; failed copies are discarded."
        ),
        "outputs": ["application/json", "application/pdf"],
        "options": {
            "mode": field("Operation", default="inspect", enum=["inspect", "encrypt", "decrypt"]),
            "new_password": field("New password", maxLength=127, secret=True),
            "owner_password": field("Optional distinct owner password", maxLength=127, secret=True),
            "allow_print": field("Allow printing", "boolean", True),
            "allow_copy": field("Allow copying", "boolean", True),
            "allow_modify": field("Allow modification and assembly", "boolean", True),
            "allow_annotate": field("Allow annotations and form filling", "boolean", True),
            "crypto_policy": field(
                "Worker crypto policy", default="compat", enum=["compat", "strict", "fips-strict"]
            ),
        },
    },
    "oxide_redact": {
        "label": "PDF Oxide text redaction (unavailable)",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": REDACTION_BLOCKED,
        "blocked_reason": REDACTION_BLOCKED,
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "regions": field(
                'JSON rectangles: [{"page":1,"rect":[x0,y0,x1,y1]}] in PDF user space',
                default="[]",
                multiline=True,
                maxLength=16000,
            ),
        },
    },
    "oxide_sanitize": {
        "label": "PDF Oxide document-level cleanup",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Strip selected catalog metadata, document JavaScript and attachment name trees "
            "in a copy. Page/field actions and shared attachment annotations can remain. "
            "This is limited cleanup, not a guarantee that a PDF is safe or anonymous."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "scrub_metadata": field("Remove document Info and catalog XMP", "boolean", True),
            "remove_javascript": field("Remove document action carriers", "boolean", True),
            "remove_embedded_files": field("Remove attachment name trees", "boolean", True),
        },
    },
    "oxide_compliance": {
        "label": "PDF Oxide compliance checks and PDF/A conversion",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Run native PDF/A, PDF/UA-1 and PDF/X checks, or attempt a PDF/A copy. "
            "Results are engine findings, not independent standards certification. "
            "Conversion may change appearance and requires source/output review."
        ),
        "outputs": ["application/json", "application/pdf"],
        "options": {
            "mode": field("Operation", default="validate", enum=["validate", "convert_pdf_a"]),
            "standard": field(
                "Validation standard", default="pdf_a", enum=["pdf_a", "pdf_ua", "pdf_x"]
            ),
            "pdf_a_level": field(
                "PDF/A level", default="2b", enum=["1a", "1b", "2a", "2b", "2u", "3a", "3b", "3u"]
            ),
            "pdf_x_level": field("PDF/X level", default="4", enum=["1a_2001", "3_2002", "4"]),
        },
    },
}


def _regions(value):
    try:
        regions = json.loads(value)
    except ValueError, TypeError:
        raise JobError("Redaction regions must be a JSON array") from None
    if not isinstance(regions, list) or not 1 <= len(regions) <= MAX_REGIONS:
        raise JobError("Redaction requires between 1 and 100 rectangles")
    normalized = []
    for item in regions:
        if not isinstance(item, dict) or set(item) != {"page", "rect"}:
            raise JobError("Each redaction region requires only page and rect")
        page, rect = item["page"], item["rect"]
        if type(page) is not int or not 1 <= page <= MAX_DOCUMENT_PAGES:
            raise JobError("Redaction page must be between 1 and 100")
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in rect)
            or not -14400 <= rect[0] < rect[2] <= 14400
            or not -14400 <= rect[1] < rect[3] <= 14400
        ):
            raise JobError("Redaction rect must be finite ordered PDF user-space coordinates")
        normalized.append({"page": page, "rect": rect})
    return normalized


def validate_options(operation, options):
    if operation == "oxide_security":
        if options["mode"] == "encrypt":
            if not 8 <= len(options["new_password"].encode("utf-8")) <= 127:
                raise JobError("New PDF password must contain 8 to 127 UTF-8 bytes")
            if (
                options["owner_password"]
                and not 8 <= len(options["owner_password"].encode("utf-8")) <= 127
            ):
                raise JobError("Owner password must contain 8 to 127 UTF-8 bytes")
        elif options["new_password"] or options["owner_password"]:
            raise JobError("New and owner passwords apply only to encryption")
    elif operation == "oxide_redact":
        _regions(options["regions"])
    elif (
        operation == "oxide_compliance"
        and options["mode"] == "convert_pdf_a"
        and options["standard"] != "pdf_a"
    ):
        raise JobError("The pinned binding converts only PDF/A, not PDF/UA or PDF/X")
    return options


def _bounded_report(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_REPORT_BYTES:
        raise JobError("PDF Oxide security report exceeds its size limit")
    return value


def _crypto(oxide):
    return _bounded_report(
        {
            "available_providers": native("crypto providers", oxide.crypto_available_providers),
            "active_provider": native("crypto provider", oxide.crypto_active_provider),
            "policy": native("crypto policy", oxide.crypto_policy),
            "inventory": native("crypto inventory", oxide.crypto_inventory),
            "cbom": json.loads(native("crypto bill of materials", oxide.crypto_cbom)),
        }
    )


def _verification(signature, data):
    try:
        valid = signature.verify_detached(data)
    except NotImplementedError:
        return {"status": "unsupported", "valid": None}
    except ValueError, RuntimeError:
        return {"status": "malformed_or_unverifiable", "valid": None}
    return {"status": "valid" if valid else "invalid", "valid": valid}


def _signatures(doc, data):
    count = native("signature count", doc.signature_count)
    if not 0 <= count <= MAX_SIGNATURES:
        raise JobError("PDF Oxide signature-count limit exceeded")
    signatures = native("signature enumeration", doc.signatures)
    if len(signatures) != count:
        raise JobError("PDF Oxide signature enumeration/count mismatch")
    records = []
    for index, signature in enumerate(signatures, 1):
        records.append(
            {
                "index": index,
                "signer_name": signature.signer_name,
                "reason": signature.reason,
                "location": signature.location,
                "contact_info": signature.contact_info,
                "signing_time": signature.signing_time,
                "covers_whole_document": signature.covers_whole_document,
                "pades_level": str(signature.pades_level),
                "pades_level_code": int(signature.pades_level),
                "detached_verification": _verification(signature, data),
                "certificate_trust": "not_checked",
                "revocation": "not_checked",
                "timestamp_trust": "not_checked",
            }
        )
        _bounded_report(records)
    return records


def _public_certificate(oxide, blob):
    try:
        certificate = oxide.Certificate.load(blob)
        return {
            "status": "parsed",
            "subject": certificate.subject,
            "issuer": certificate.issuer,
            "serial": certificate.serial,
            "validity": certificate.validity,
            "within_time_window": certificate.is_valid,
            "trust_chain": "not_checked",
            "revocation": "not_checked",
        }
    except ValueError, RuntimeError, NotImplementedError:
        return {"status": "unparseable_or_unsupported", "trust_chain": "not_checked"}


def _dss(doc, oxide):
    store = native("document security store", doc.dss)
    if store is None:
        return None
    report = {"vri": store.vri, "verification": "not_checked"}
    total = 0
    for key in ("certs", "crls", "ocsps"):
        blobs = getattr(store, key)
        if len(blobs) > MAX_SIGNATURES:
            raise JobError("PDF Oxide document-security-store count limit exceeded")
        report[key] = []
        for blob in blobs:
            total += len(blob)
            if total > MAX_REPORT_BYTES:
                raise JobError("PDF Oxide document-security-store byte limit exceeded")
            item = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            if key == "certs":
                item["public_certificate_metadata"] = _public_certificate(oxide, blob)
            report[key].append(item)
    return _bounded_report(report)


def _timestamps(data, password):
    """Inspect document timestamp dictionaries without contacting a TSA or trust service."""
    import pikepdf
    from pdf_oxide.pdf_oxide import Timestamp

    records = []
    total = 0
    with _independent_pdf(data, password) as pdf:
        for obj in pdf.objects:
            if (
                not isinstance(obj, pikepdf.Dictionary)
                or obj.get("/Type") != pikepdf.Name.DocTimeStamp
            ):
                continue
            if len(records) >= MAX_SIGNATURES:
                raise JobError("PDF Oxide timestamp count limit exceeded")
            contents = obj.get("/Contents")
            if not isinstance(contents, pikepdf.String):
                records.append({"status": "missing_or_nonstring_contents"})
                continue
            blob = bytes(contents)
            total += len(blob)
            if total > MAX_REPORT_BYTES:
                raise JobError("PDF Oxide timestamp byte limit exceeded")
            record = {
                "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "trust_chain": "not_checked",
                "document_imprint_match": "not_checked",
            }
            try:
                timestamp = Timestamp.parse(blob)
                record.update(
                    {
                        "status": "parsed",
                        "time": timestamp.time,
                        "serial": timestamp.serial,
                        "policy_oid": timestamp.policy_oid,
                        "tsa_name": timestamp.tsa_name,
                        "hash_algorithm": timestamp.hash_algorithm,
                        "message_imprint_hex": bytes(timestamp.message_imprint).hex(),
                    }
                )
                try:
                    record["token_signature_valid"] = timestamp.verify()
                except ValueError, RuntimeError, NotImplementedError:
                    record["token_signature_valid"] = None
            except ValueError, RuntimeError, NotImplementedError:
                record["status"] = "unparseable_or_unsupported"
            records.append(record)
            _bounded_report(records)
    return records


def _serialize(doc):
    return native(
        "full rewrite", lambda: doc.to_bytes(compress=True, garbage_collect=True, linearize=False)
    )


def _independent_pdf(data, password=""):
    import pikepdf

    return native("independent PDF open", lambda: pikepdf.open(io.BytesIO(data), password=password))


def _signature_identity_coverage(pdf, signature_ids, direct_signatures):
    """Compare AcroForm value identities with independently discovered objects.

    The native binding returns field records without PDF object identities. A
    repeated reference therefore cannot count as coverage of a different object.
    Direct dictionaries and malformed/repeated field links remain ambiguous.
    """
    import pikepdf

    linked = {}
    seen_fields = set()
    coverage = {
        "signature_field_references": 0,
        "duplicate_signature_references": 0,
        "unresolved_signature_fields": 0,
        "repeated_field_links": 0,
        "direct_signature_dictionaries": direct_signatures,
    }
    visited = 0

    def walk(field, depth=0):
        nonlocal visited
        visited += 1
        if visited > 200_000 or depth > 64:
            raise JobError("PDF Oxide signature-field traversal limit exceeded")
        if not isinstance(field, pikepdf.Dictionary):
            coverage["unresolved_signature_fields"] += 1
            return
        if field.is_indirect:
            if field.objgen in seen_fields:
                coverage["repeated_field_links"] += 1
                return
            seen_fields.add(field.objgen)
        if field.get("/FT") == pikepdf.Name.Sig and field.get("/V") is not None:
            coverage["signature_field_references"] += 1
            value = field["/V"]
            if (
                isinstance(value, pikepdf.Dictionary)
                and value.is_indirect
                and value.objgen in signature_ids
            ):
                linked[value.objgen] = linked.get(value.objgen, 0) + 1
            else:
                coverage["unresolved_signature_fields"] += 1
        kids = field.get("/Kids")
        if kids is not None:
            if not isinstance(kids, pikepdf.Array):
                coverage["unresolved_signature_fields"] += 1
            else:
                for child in kids:
                    walk(child, depth + 1)

    acroform = pdf.Root.get("/AcroForm")
    if acroform is not None:
        if not isinstance(acroform, pikepdf.Dictionary):
            coverage["unresolved_signature_fields"] += 1
        else:
            fields = acroform.get("/Fields")
            if fields is not None:
                if not isinstance(fields, pikepdf.Array):
                    coverage["unresolved_signature_fields"] += 1
                else:
                    for field in fields:
                        walk(field)
    coverage["duplicate_signature_references"] = sum(count - 1 for count in linked.values())
    coverage["signature_object_ids"] = sorted(signature_ids)
    coverage["linked_signature_object_ids"] = sorted(linked)
    coverage["unlinked_signature_object_ids"] = sorted(signature_ids - linked.keys())
    return coverage


def _independent_inventory(data, password=""):
    """Count residual carriers without returning action text or credential material."""
    import pikepdf

    report = {
        "signature_dictionaries": 0,
        "embedded_file_streams": 0,
        "action_dictionaries": 0,
        "associated_file_arrays": 0,
        "metadata_streams": 0,
    }
    with _independent_pdf(data, password) as pdf:
        if len(pdf.objects) > 50_000:
            raise JobError("PDF Oxide independent object inventory limit exceeded")
        seen = set()
        visited = 0
        signature_ids = set()
        direct_signatures = 0

        def visit(obj, depth=0):
            nonlocal visited, direct_signatures
            visited += 1
            if visited > 200_000:
                raise JobError("PDF Oxide independent object traversal limit exceeded")
            if depth > 64:
                raise JobError("PDF Oxide independent object nesting limit exceeded")
            if not isinstance(obj, pikepdf.Dictionary | pikepdf.Stream | pikepdf.Array):
                return
            if obj.is_indirect:
                if obj.objgen in seen:
                    return
                seen.add(obj.objgen)
            if isinstance(obj, pikepdf.Array):
                for child in obj:
                    visit(child, depth + 1)
                return
            if obj.get("/Type") in {pikepdf.Name.Sig, pikepdf.Name.DocTimeStamp} or (
                "/ByteRange" in obj and "/Contents" in obj
            ):
                report["signature_dictionaries"] += 1
                if obj.is_indirect:
                    signature_ids.add(obj.objgen)
                else:
                    direct_signatures += 1
            if obj.get("/Type") == pikepdf.Name.EmbeddedFile:
                report["embedded_file_streams"] += 1
            if obj.get("/Type") == pikepdf.Name.Metadata:
                report["metadata_streams"] += 1
            if obj.get("/S") in {
                pikepdf.Name.JavaScript,
                pikepdf.Name.Launch,
                pikepdf.Name.SubmitForm,
                pikepdf.Name.ImportData,
            }:
                report["action_dictionaries"] += 1
            if "/AF" in obj:
                report["associated_file_arrays"] += 1
            for _, child in obj.items():
                visit(child, depth + 1)

        for obj in pdf.objects:
            visit(obj)
        report["signature_identity_coverage"] = _signature_identity_coverage(
            pdf, signature_ids, direct_signatures
        )
    return report


def _security(request, options, add):
    oxide = require_runtime(request)
    policy = options["crypto_policy"]
    if policy == "fips-strict":
        if "aws-lc-rs" not in native("crypto providers", oxide.crypto_available_providers):
            raise JobError("The pinned PDF Oxide wheel has no FIPS provider; request refused")
        native("FIPS provider selection", oxide.crypto_use_fips)
    if policy != "compat":
        native("worker crypto policy", lambda: oxide.crypto_set_policy(policy))
    doc, data, source = open_source(request, options)
    independent = _independent_inventory(data, options["password"])
    signatures = _signatures(doc, data)
    coverage = independent["signature_identity_coverage"]
    coverage["native_signature_records"] = len(signatures)
    coverage["complete"] = not any(
        (
            coverage["unlinked_signature_object_ids"],
            coverage["duplicate_signature_references"],
            coverage["unresolved_signature_fields"],
            coverage["repeated_field_links"],
            coverage["direct_signature_dictionaries"],
            coverage["signature_field_references"] != len(signatures),
        )
    )
    permissions = native("permissions", doc.permissions)
    report = {
        "source": source,
        "mode": options["mode"],
        "encrypted_source": permissions is not None,
        "permissions": permissions,
        "permissions_enforced_by_engine": False,
        "signatures": signatures,
        "document_security_store": _dss(doc, oxide),
        "document_timestamps": _timestamps(data, options["password"]),
        "document_timestamp_present": native(
            "document timestamp presence", lambda: oxide.has_document_timestamp(data)
        ),
        "document_timestamp_verified": False,
        "enumeration_scope": "AcroForm-linked signature fields",
        "independent_object_inventory": independent,
        "unlinked_signature_dictionaries": len(coverage["unlinked_signature_object_ids"]),
        "signature_identity_coverage_complete": coverage["complete"],
    }
    warnings = ["Permission flags are advisory; signature verification does not establish trust."]
    partial = any(item["detached_verification"]["status"] != "valid" for item in signatures)
    if not coverage["complete"]:
        partial = True
        warnings.append(
            "Signature object identities do not have unambiguous complete coverage: "
            "unlinked dictionaries, duplicate references or unresolved fields remain. "
            "Verification is incomplete."
        )
    if options["mode"] != "inspect":
        if source["pages"] > MAX_DOCUMENT_PAGES:
            raise JobError("PDF Oxide security rewriting is limited to 100 pages")
        if signatures:
            warnings.append("The rewritten copy does not preserve existing signature validity.")
        if options["mode"] == "encrypt":
            output = native(
                "AES-256 encryption",
                lambda: doc.to_bytes_encrypted(
                    options["new_password"],
                    owner_password=options["owner_password"] or None,
                    **{
                        key: options[key]
                        for key in ("allow_print", "allow_copy", "allow_modify", "allow_annotate")
                    },
                ),
            )
            with _independent_pdf(output, options["new_password"]) as verified:
                if not verified.is_encrypted or verified.encryption.bits != 256:
                    raise JobError("Independent AES-256 output verification failed")
                if len(verified.pages) != source["pages"]:
                    raise JobError("Encrypted output page count changed")
                report["output_encryption"] = {
                    "bits": verified.encryption.bits,
                    "R": verified.encryption.R,
                }
            report["owner_password_distinct"] = bool(
                options["owner_password"] and options["owner_password"] != options["new_password"]
            )
        else:
            if permissions is None:
                raise JobError("Decryption requires an authenticated encrypted source")
            output = _serialize(doc)
            with _independent_pdf(output) as verified:
                if verified.is_encrypted or len(verified.pages) != source["pages"]:
                    raise JobError("Independent decrypted-output verification failed")
        write_bytes(request, add, "oxide-security.pdf", output)
        report["output_sha256"] = hashlib.sha256(output).hexdigest()
    report["crypto"] = _crypto(oxide)
    write_json(request, add, "oxide-security.json", _bounded_report(report))
    return warnings, partial


def _redact(request, options, add):
    # Native success and an opaque rectangle do not prove irreversible removal.
    raise JobError(REDACTION_BLOCKED)


def _sanitize(request, options, add):
    require_runtime(request)
    doc, _, source = open_source(request, options)
    if source["pages"] > MAX_DOCUMENT_PAGES:
        raise JobError("PDF Oxide document cleanup is limited to 100 pages")
    settings = {
        key: options[key]
        for key in ("scrub_metadata", "remove_javascript", "remove_embedded_files")
    }
    result = native("document cleanup", lambda: doc.sanitize_document(**settings))
    output = _serialize(doc)
    with _independent_pdf(output) as verified:
        if len(verified.pages) != source["pages"]:
            raise JobError("Sanitized output page count changed")
    inventory = _independent_inventory(output)
    residual = bool(
        (settings["remove_javascript"] and inventory["action_dictionaries"])
        or (
            settings["remove_embedded_files"]
            and (inventory["embedded_file_streams"] or inventory["associated_file_arrays"])
        )
        or (settings["scrub_metadata"] and inventory["metadata_streams"])
    )
    write_bytes(request, add, "oxide-catalog-cleaned.pdf", output)
    write_json(
        request,
        add,
        "oxide-catalog-cleaned.json",
        {
            "source": source,
            "settings": settings,
            "native_report": result,
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "complete_sanitization": False,
            "independent_residual_inventory": inventory,
            "status": "residual_carriers_detected" if residual else "catalog_cleanup_performed",
            "scope": "Info, catalog XMP, catalog actions and JavaScript/EmbeddedFiles name trees",
            "possible_retained_content": [
                "page and field actions",
                "shared attachment annotations",
                "associated files",
                "page and image metadata",
                "hidden layers",
            ],
            "signature_validity_preserved": False,
        },
    )
    return [
        "Document-level cleanup can retain active content and shared attachments; "
        "review the report."
    ], residual


def _compliance(request, options, add):
    oxide = require_runtime(request)
    doc, _, source = open_source(request, options)
    if source["pages"] > MAX_DOCUMENT_PAGES:
        raise JobError("PDF Oxide compliance checks are limited to 100 pages")
    report = {"source": source, "independent_certification": False}
    if options["mode"] == "convert_pdf_a":
        conversion = native(
            "PDF/A conversion", lambda: doc.convert_to_pdf_a(options["pdf_a_level"])
        )
        output = _serialize(doc)
        # Reopen the actual serialized copy; converter success may have downgraded
        # font errors and its own validation object is not exposed by this binding.
        reopened = native("converted PDF reopen", lambda: oxide.PdfDocument.from_bytes(output, ""))
        postcheck = native(
            "serialized PDF/A check", lambda: reopened.validate_pdf_a(options["pdf_a_level"])
        )
        report.update({"conversion": conversion, "serialized_output_validation": postcheck})
        report["status"] = (
            "native_checks_passed"
            if conversion["success"] and postcheck["valid"]
            else "incomplete_conversion"
        )
        report["output_sha256"] = hashlib.sha256(output).hexdigest()
        with _independent_pdf(output) as verified:
            if len(verified.pages) != source["pages"]:
                raise JobError("PDF/A output page count changed")
        write_bytes(request, add, "oxide-pdfa-candidate.pdf", output)
        partial = report["status"] == "incomplete_conversion"
    else:
        if options["standard"] == "pdf_a":
            result = native("PDF/A validation", lambda: doc.validate_pdf_a(options["pdf_a_level"]))
        elif options["standard"] == "pdf_ua":
            result = native("PDF/UA-1 validation", doc.validate_pdf_ua)
        else:
            result = native("PDF/X validation", lambda: doc.validate_pdf_x(options["pdf_x_level"]))
        report.update({"standard": options["standard"], "native_validation": result})
        report["status"] = "native_checks_passed" if result["valid"] else "native_checks_failed"
        partial = False  # A negative validation is a completed inspection.
    write_json(request, add, "oxide-compliance.json", _bounded_report(report))
    return ["Native compliance checks are not independent standards certification."], partial


def process(request, options, add):
    validate_options(request["operation"], options)
    return {
        "oxide_security": _security,
        "oxide_redact": _redact,
        "oxide_sanitize": _sanitize,
        "oxide_compliance": _compliance,
    }[request["operation"]](request, options, add)
