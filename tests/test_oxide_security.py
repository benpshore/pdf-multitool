"""Actual pinned security operations, with independent inspection of derived PDFs."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from pdf_multitool import oxide
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import Job, JobError, Limits
from pdf_multitool.oxide_security import _regions
from tests.test_extended import result, run

NATIVE = pytest.mark.skipif(not oxide.availability()["available"], reason="Optional bounded Oxide")


def text_source(path):
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((50, 100), "SECRET 012 10.25")
        page.insert_text((50, 200), "PUBLIC 036 -2.00")
        doc.save(path)
    return path


def json_result(job, name):
    return json.loads(result(job, name).read_text())


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "null",
        "{}",
        "[{}]",
        '[{"page":1,"rect":[0,0,0,10]}]',
        '[{"page":true,"rect":[0,0,10,10]}]',
        '[{"page":101,"rect":[0,0,10,10]}]',
        '[{"page":1,"rect":[0,0,NaN,10]}]',
        '[{"page":1,"rect":[0,0,10,10],"extra":1}]',
    ],
)
def test_invalid_regions_before_spawn(value, tmp_path):
    with pytest.raises(JobError):
        _regions(value)
    with pytest.raises(JobError):
        Job("oxide_redact", [{"id": "f"}], {"regions": value}, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


def test_security_password_rules():
    for options in (
        {"mode": "encrypt"},
        {"mode": "encrypt", "new_password": "x" * 128},
        {"mode": "encrypt", "new_password": "é" * 70},
        {"new_password": "unneeded"},
    ):
        with pytest.raises(JobError):
            validate_options("oxide_security", options)
    with pytest.raises(JobError):
        validate_options("oxide_compliance", {"mode": "convert_pdf_a", "standard": "pdf_x"})


@NATIVE
def test_security_inspection_preserves_original_and_admits_trust_gap(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_security", source)
    assert job.state == "completed", job.errors
    report = json_result(job, "oxide-security.json")
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["signatures"] == []
    assert report["permissions"] is None
    assert report["document_security_store"] is None
    assert report["document_timestamp_verified"] is False
    assert report["crypto"]["available_providers"] == ["rust-crypto"]
    assert report["crypto"]["policy"] == "compat"
    assert report["crypto"]["cbom"]["bomFormat"] == "CycloneDX"


@NATIVE
def test_known_native_encryption_rejected_and_input_permissions_inspected(tmp_path, monkeypatch):
    source = text_source(tmp_path / "source.pdf")
    user_password = "synthetic-user-secret"  # noqa: S105 -- public synthetic test value
    owner_password = "synthetic-owner-secret"  # noqa: S105 -- public synthetic test value
    argv = []
    popen = subprocess.Popen

    def observe(args, **kwargs):
        argv.append(args)
        return popen(args, **kwargs)

    monkeypatch.setattr("pdf_multitool.core.subprocess.Popen", observe)
    job = run(
        tmp_path,
        "oxide_security",
        source,
        {
            "mode": "encrypt",
            "new_password": user_password,
            "owner_password": owner_password,
            "allow_copy": False,
            "allow_modify": False,
            "allow_print": False,
            "allow_annotate": False,
        },
    )
    # 0.3.78 writes an incomplete V=5 encryption dictionary. MuPDF tolerates it;
    # qpdf rejects it. The adapter must not publish a successful encryption result.
    assert job.state == "failed" and not job.results, job.snapshot()
    assert "independent PDF open" in str(job.errors)
    encrypted = tmp_path / "independent-encrypted.pdf"
    with pikepdf.open(source) as pdf:
        pdf.save(
            encrypted,
            encryption=pikepdf.Encryption(
                user=user_password,
                owner=owner_password,
                R=6,
                allow=pikepdf.Permissions(
                    extract=False,
                    print_lowres=False,
                    print_highres=False,
                    modify_annotation=False,
                    modify_assembly=False,
                ),
            ),
        )
    with pytest.raises(pikepdf.PasswordError), pikepdf.open(encrypted):
        pass
    with pikepdf.open(encrypted, password=user_password) as doc:
        assert doc.encryption.bits == 256 and doc.encryption.R == 6
        assert not doc.allow.extract and not doc.allow.print_lowres
        assert not doc.allow.modify_annotation and not doc.allow.modify_assembly
    with pymupdf.open(encrypted) as doc:
        assert doc.authenticate(user_password)
        assert "SECRET 012 10.25" in doc[0].get_text()
        assert "PUBLIC 036 -2.00" in doc[0].get_text()
    inspect = run(tmp_path, "oxide_security", encrypted, {"password": user_password})
    assert inspect.state == "completed", inspect.errors
    assert json_result(inspect, "oxide-security.json")["permissions"]["copy"] is False
    decrypted = run(
        tmp_path, "oxide_security", encrypted, {"mode": "decrypt", "password": user_password}
    )
    # The native editor reopens raw encrypted bytes without the authenticated
    # reader's password. Keep this release limitation visible, with no output.
    assert decrypted.state == "failed" and not decrypted.results, decrypted.snapshot()
    for tested in (job, inspect, decrypted):
        assert not tested.options.get("new_password")
        assert not tested.options.get("owner_password")
        assert not tested.options.get("password")
        for output in tested.results:
            if output["path"].endswith(".json"):
                text = Path(output["path"]).read_text()
                assert user_password not in text and owner_password not in text
    assert all(
        user_password not in str(command) and owner_password not in str(command) for command in argv
    )


@NATIVE
def test_no_fips_fallback_and_plaintext_decrypt_rejected(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    for options, fragment in (
        ({"crypto_policy": "fips-strict"}, "FIPS"),
        ({"mode": "decrypt"}, "encrypted"),
    ):
        job = run(tmp_path, "oxide_security", source, options)
        assert job.state == "failed" and not job.results
        assert fragment in str(job.errors)
        assert not list(job.directory.iterdir())
    strict = run(tmp_path, "oxide_security", source, {"crypto_policy": "strict"})
    assert strict.state == "completed", strict.errors
    assert json_result(strict, "oxide-security.json")["crypto"]["policy"] == "strict"
    compatible = run(tmp_path, "oxide_security", source)
    assert json_result(compatible, "oxide-security.json")["crypto"]["policy"] == "compat"


@NATIVE
def test_document_security_store_inventory_is_bounded_and_does_not_export_blobs(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    marker = b"Synthetic public diagnostic blob; no real certificate"
    with pikepdf.open(source) as pdf:
        pdf.Root.DSS = pikepdf.Dictionary(
            Certs=pikepdf.Array([pdf.make_stream(marker)]),
            CRLs=pikepdf.Array([pdf.make_stream(b"Synthetic CRL")]),
            OCSPs=pikepdf.Array([pdf.make_stream(b"Synthetic OCSP")]),
            VRI=pikepdf.Dictionary(),
        )
        dss = tmp_path / "dss.pdf"
        pdf.save(dss)
    job = run(tmp_path, "oxide_security", dss)
    assert job.state == "completed", job.errors
    report = json_result(job, "oxide-security.json")["document_security_store"]
    assert report["verification"] == "not_checked"
    assert report["certs"] == [
        {
            "bytes": len(marker),
            "sha256": hashlib.sha256(marker).hexdigest(),
            "public_certificate_metadata": {
                "status": "unparseable_or_unsupported",
                "trust_chain": "not_checked",
            },
        }
    ]
    assert marker.decode() not in result(job, "oxide-security.json").read_text()


@NATIVE
def test_signature_count_limit_prevents_large_verification_fanout(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    with pikepdf.open(source) as pdf:
        signature = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Sig,
                Contents=pikepdf.String(b"malformed synthetic CMS"),
                ByteRange=pikepdf.Array([0, 10, 20, 10]),
            )
        )
        fields = [
            pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, V=signature))
            for _ in range(101)
        ]
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(fields))
        signed = tmp_path / "many-signatures.pdf"
        pdf.save(signed)
    job = run(tmp_path, "oxide_security", signed)
    assert job.state == "failed" and not job.results
    assert "signature-count limit" in str(job.errors)


@NATIVE
def test_timestamp_metadata_is_not_a_trusted_document_timestamp(tmp_path):
    def der(tag, value):
        assert len(value) < 128
        return bytes([tag, len(value)]) + value

    # Bare TSTInfo: version 1, policy 1.2.3.4, SHA256 imprint, serial 1,
    # generation time 2026-01-01. No CMS signature or trusted TSA is asserted.
    algorithm = der(0x30, der(0x06, bytes.fromhex("608648016503040201")) + b"\x05\x00")
    imprint = der(0x30, algorithm + der(0x04, b"\x12" * 32))
    token = der(
        0x30,
        b"\x02\x01\x01"
        + der(0x06, b"\x2a\x03\x04")
        + imprint
        + b"\x02\x01\x01"
        + der(0x18, b"20260101000000Z"),
    )
    source = text_source(tmp_path / "source.pdf")
    with pikepdf.open(source) as pdf:
        pdf.Root.SyntheticTimestamp = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.DocTimeStamp,
                Contents=pikepdf.String(token),
                ByteRange=pikepdf.Array([0, 0, 0, 0]),
            )
        )
        timestamped = tmp_path / "timestamp-metadata.pdf"
        pdf.save(timestamped)
    job = run(tmp_path, "oxide_security", timestamped)
    assert job.state == "partial", job.errors
    records = json_result(job, "oxide-security.json")["document_timestamps"]
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "parsed" and record["policy_oid"] == "1.2.3.4"
    assert record["hash_algorithm"] == 2 and record["message_imprint_hex"] == "12" * 32
    assert record["token_signature_valid"] is None
    assert record["trust_chain"] == record["document_imprint_match"] == "not_checked"


@NATIVE
def test_redaction_resolves_inherited_rotation_before_any_output(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    with pikepdf.open(source) as pdf:
        if "/Rotate" in pdf.pages[0].obj:
            del pdf.pages[0].obj.Rotate
        pdf.pages[0].obj.Parent.Rotate = 90
        inherited = tmp_path / "inherited-rotation.pdf"
        pdf.save(inherited)
    job = run(
        tmp_path, "oxide_redact", inherited, {"regions": '[{"page":1,"rect":[40,285,200,320]}]'}
    )
    assert job.state == "failed" and not job.results
    assert "unrotated" in str(job.errors)


@NATIVE
def test_text_redaction_removes_content_and_preserves_other_numbers(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    regions = json.dumps([{"page": 1, "rect": [40, 285, 200, 320]}])
    job = run(tmp_path, "oxide_redact", source, {"regions": regions})
    assert job.state == "completed", job.errors
    output = result(job, "oxide-text-redacted.pdf")
    with pymupdf.open(output) as doc:
        assert "SECRET" not in doc[0].get_text()
        assert "012" not in doc[0].get_text() and "10.25" not in doc[0].get_text()
        assert "PUBLIC 036 -2.00" in doc[0].get_text()
    with pikepdf.open(output) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                assert b"SECRET" not in obj.read_bytes()
    report = json_result(job, "oxide-text-redacted.json")
    assert report["native_redaction"]["glyphs_removed"] > 0
    assert report["secure_redaction_certified"] is False


@NATIVE
@pytest.mark.parametrize("kind", ["image", "vector", "annotation", "rotation", "form"])
def test_redaction_refuses_unqualified_content_and_keeps_no_overlay_copy(tmp_path, kind):
    source = text_source(tmp_path / "source.pdf")
    with pymupdf.open(source) as doc:
        if kind == "image":
            image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
            image.clear_with(100)
            doc[0].insert_image(pymupdf.Rect(50, 250, 100, 300), pixmap=image)
        elif kind == "vector":
            doc[0].draw_rect(pymupdf.Rect(50, 250, 100, 300), fill=(1, 0, 0))
        elif kind == "annotation":
            doc[0].add_text_annot((50, 250), "Sensitive annotation")
        elif kind == "rotation":
            doc[0].set_rotation(90)
        else:
            widget = pymupdf.Widget()
            widget.field_name = "secret"
            widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            widget.rect = pymupdf.Rect(50, 250, 150, 300)
            widget.field_value = "secret form value"
            doc[0].add_widget(widget)
        edited = tmp_path / "edited.pdf"
        doc.save(edited)
    job = run(tmp_path, "oxide_redact", edited, {"regions": '[{"page":1,"rect":[40,285,200,320]}]'})
    assert job.state == "failed" and not job.results, job.snapshot()
    assert not list(job.directory.iterdir())


@NATIVE
def test_catalog_cleanup_removes_catalog_carriers_and_reports_remaining_scope(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    with pikepdf.open(source) as pdf:
        pdf.docinfo["/Title"] = "SYNTHETIC SECRET METADATA"
        pdf.Root.Metadata = pdf.make_stream(b"SYNTHETIC SECRET XMP")
        pdf.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.JavaScript, JS="document-script-token"
        )
        pdf.pages[0].obj.AA = pikepdf.Dictionary(
            O=pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="page-script-token")
        )
        pdf.attachments["synthetic.txt"] = b"SYNTHETIC SECRET ATTACHMENT"
        active = tmp_path / "active.pdf"
        pdf.save(active)
    job = run(tmp_path, "oxide_sanitize", active)
    assert job.state == "partial", job.errors
    output = result(job, "oxide-catalog-cleaned.pdf")
    with pikepdf.open(output) as pdf:
        assert not pdf.docinfo
        assert "/Metadata" not in pdf.Root and "/OpenAction" not in pdf.Root
        assert len(pdf.attachments) == 0
        assert str(pdf.pages[0].obj.AA.O.JS) == "page-script-token"
        assert any(
            isinstance(obj, pikepdf.Stream) and b"SYNTHETIC SECRET ATTACHMENT" in obj.read_bytes()
            for obj in pdf.objects
        ), "Freeze the native associated-file retention failure"
    with pymupdf.open(output) as pdf:
        assert "PUBLIC 036 -2.00" in pdf[0].get_text()
    report = json_result(job, "oxide-catalog-cleaned.json")
    assert report["complete_sanitization"] is False
    assert "page and field actions" in report["possible_retained_content"]
    assert report["status"] == "residual_carriers_detected"
    assert report["independent_residual_inventory"]["embedded_file_streams"] == 1
    assert report["independent_residual_inventory"]["action_dictionaries"] == 1


@NATIVE
@pytest.mark.parametrize("standard", ["pdf_a", "pdf_ua", "pdf_x"])
def test_native_compliance_negative_is_inspection_result_not_certification(tmp_path, standard):
    source = text_source(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_compliance", source, {"standard": standard})
    assert job.state == "completed", job.errors
    report = json_result(job, "oxide-compliance.json")
    assert report["native_validation"]["valid"] is False
    assert report["native_validation"]["errors"]
    assert report["independent_certification"] is False


@NATIVE
def test_pdfa_conversion_reopens_actual_output_and_retains_numbers(tmp_path):
    source = text_source(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_compliance", source, {"mode": "convert_pdf_a"})
    assert job.state in {"completed", "partial"}, job.errors
    report = json_result(job, "oxide-compliance.json")
    assert "serialized_output_validation" in report
    assert report["independent_certification"] is False
    with pymupdf.open(result(job, "oxide-pdfa-candidate.pdf")) as pdf:
        assert "SECRET 012 10.25" in pdf[0].get_text()
        assert "PUBLIC 036 -2.00" in pdf[0].get_text()


@NATIVE
@pytest.mark.parametrize(
    "operation,options",
    [
        ("oxide_security", {}),
        ("oxide_compliance", {}),
        ("oxide_sanitize", {}),
        ("oxide_redact", {"regions": '[{"page":1,"rect":[40,285,200,320]}]'}),
    ],
)
def test_output_overflow_does_not_publish_partial_artifacts(tmp_path, operation, options):
    source = text_source(tmp_path / "source.pdf")
    job = run(tmp_path, operation, source, options, replace(Limits(), max_output_bytes=200))
    assert job.state == "failed" and not job.results
    assert not list(job.directory.iterdir())


SIGNED_FIXTURE = r"""
import datetime
import os
import sys
from dataclasses import asdict
from pathlib import Path
from pdf_multitool.core import Limits
from pdf_multitool.worker import _limits
from pdf_multitool.isolation import install
from pdf_multitool.oxide import initialize_guard
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.x509.oid import NameOID
_limits(asdict(Limits()), "oxide_security")
initialize_guard(install(), asdict(Limits()))
from pdf_oxide import Certificate, sign_pdf_bytes
from cryptography.hazmat.primitives.serialization import pkcs7
import io
import re
import pikepdf
algorithm = sys.argv[3]
if algorithm in {"rsa", "rsa_pss"}:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    digest = hashes.SHA256()
else:
    key = ec.generate_private_key(ec.SECP256R1() if algorithm == "ecdsa_p256" else ec.SECP384R1())
    digest = hashes.SHA256() if algorithm == "ecdsa_p256" else hashes.SHA384()
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic test only")])
now = datetime.datetime.now(datetime.UTC)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
    .public_key(key.public_key()).serial_number(x509.random_serial_number())
    .not_valid_before(now - datetime.timedelta(days=1))
    .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
# Ephemeral synthetic credentials stay in memory; only the signed fixture is written.
source_bytes = Path(sys.argv[1]).read_bytes()
if algorithm == "rsa":
    native_cert = Certificate.load_pem(
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode())
    Path(sys.argv[2]).with_name("bare-native-signature.pdf").write_bytes(
        sign_pdf_bytes(source_bytes, native_cert))
# Build a linked synthetic signature independently; native signing leaves its
# signature dictionary outside AcroForm, which native enumeration cannot see.
def cms_for(data):
    extra = {"rsa_padding": padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size)} \
        if algorithm == "rsa_pss" else {}
    return (pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert, key, digest, **extra)
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature,
                                          pkcs7.PKCS7Options.Binary]))
# The native verifier rejects usual /Contents trailing padding. Freeze that
# failure separately and use exact DER length for the supported positive case.
length = len(cms_for(b"synthetic size probe"))
def linked_signature(capacity):
    with pikepdf.open(io.BytesIO(source_bytes)) as doc:
        signature = doc.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, Filter=pikepdf.Name.Adobe_PPKLite,
            SubFilter=pikepdf.Name.adbe_pkcs7_detached,
            Contents=pikepdf.String(bytes(capacity)),
            ByteRange=pikepdf.Array([9999999999] * 4)))
        field = doc.make_indirect(pikepdf.Dictionary(
            FT=pikepdf.Name.Sig, T="Synthetic signature", V=signature))
        doc.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        doc.Root.DSS = pikepdf.Dictionary(Certs=pikepdf.Array([
            doc.make_stream(cert.public_bytes(serialization.Encoding.DER))]))
        output = io.BytesIO()
        doc.save(output, compress_streams=False)
        data = output.getvalue()
    contents = re.search(rb'/Contents\s*(<0{' + str(capacity * 2).encode() + rb'}>)', data)
    assert contents is not None
    start, end = contents.span(1)
    byte_range = re.search(rb'/ByteRange\s*\[[^\]]+\]', data)
    values = (0, start, end, len(data) - end)
    replacement = b'/ByteRange [ ' + b' '.join(str(v).encode() for v in values) + b' ]'
    replacement += b' ' * (len(byte_range.group()) - len(replacement))
    data = data[:byte_range.start()] + replacement + data[byte_range.end():]
    signed_bytes = data[:start] + data[end:]
    for _ in range(64):
        cms = cms_for(signed_bytes)
        if len(cms) == capacity or capacity > length:
            break
    assert len(cms) == capacity or capacity > length
    encoded = cms.hex().encode()
    assert len(encoded) <= capacity * 2
    return data[:start + 1] + encoded.ljust(capacity * 2, b'0') + data[end - 1:]
Path(sys.argv[2]).write_bytes(linked_signature(length))
Path(sys.argv[2]).with_name("padded-signature.pdf").write_bytes(linked_signature(length + 64))
"""


@NATIVE
@pytest.mark.parametrize("algorithm", ["rsa", "rsa_pss", "ecdsa_p256", "ecdsa_p384"])
def test_signature_detached_verification_distinguishes_tampering_from_trust(tmp_path, algorithm):
    source = text_source(tmp_path / "source.pdf")
    signed = tmp_path / "signed.pdf"
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "PYTHONPATH"}}
    made = subprocess.run(  # noqa: S603 -- fixed test program and synthetic paths, no shell
        [sys.executable, "-c", SIGNED_FIXTURE, str(source), str(signed), algorithm],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert made.returncode == 0, made.stderr.decode()
    job = run(tmp_path, "oxide_security", signed)
    assert job.state == "completed", job.errors
    signatures = json_result(job, "oxide-security.json")["signatures"]
    assert len(signatures) == 1
    assert signatures[0]["detached_verification"] == {"status": "valid", "valid": True}
    assert signatures[0]["certificate_trust"] == "not_checked"
    assert signatures[0]["pades_level_code"] == 0
    certificate = json_result(job, "oxide-security.json")["document_security_store"]["certs"][0][
        "public_certificate_metadata"
    ]
    assert certificate["status"] == "parsed"
    assert "Synthetic test only" in certificate["subject"]
    assert certificate["subject"] == certificate["issuer"]
    assert certificate["within_time_window"] is True
    assert certificate["trust_chain"] == "not_checked"
    if algorithm == "rsa":
        bare = run(tmp_path, "oxide_security", signed.with_name("bare-native-signature.pdf"))
        assert bare.state == "partial", bare.errors
        bare_report = json_result(bare, "oxide-security.json")
        assert bare_report["signatures"] == []
        assert bare_report["unlinked_signature_dictionaries"] == 1
    padded = run(tmp_path, "oxide_security", signed.with_name("padded-signature.pdf"))
    assert padded.state == "partial", padded.errors
    assert json_result(padded, "oxide-security.json")["signatures"][0]["detached_verification"] == {
        "status": "malformed_or_unverifiable",
        "valid": None,
    }
    changed = tmp_path / "tampered.pdf"
    data = signed.read_bytes()
    # Same-byte-count field-label change keeps PDF/xref parseable and invalidates the signature.
    assert b"Synthetic signature" in data
    changed.write_bytes(data.replace(b"Synthetic signature", b"Synthetic signXture", 1))
    tampered = run(tmp_path, "oxide_security", changed)
    assert tampered.state == "partial", tampered.errors
    assert json_result(tampered, "oxide-security.json")["signatures"][0][
        "detached_verification"
    ] == {"status": "invalid", "valid": False}
